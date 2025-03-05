from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.base.exceptions import TwilioRestException
import openai
import os
import requests
from dotenv import load_dotenv
import threading
import time as time_module  # Rename to avoid conflict with datetime.time
import base64
import json
from datetime import datetime, timezone, timedelta, time
import re
import queue
import logging
import pytz

# Import from our new modules
from utils.database import supabase
from agents.general_agent.general_db import store_conversation, get_conversation_history
from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder, 
    get_pending_reminders, get_late_reminders,
    format_reminder_list_by_time, format_created_reminders,
    to_local_timezone, to_utc_timezone, format_datetime,
    BRAZIL_TIMEZONE
)
from agents.general_agent.general_agent import get_ai_response, handle_message, get_conversation_context
from agents.reminder_agent.reminder_agent import ReminderAgent

# Configure logging
log_level = os.getenv('LOG_LEVEL', 'INFO')
health_log_level = os.getenv('HEALTH_LOG_LEVEL', 'DEBUG')

logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure health check logger
health_logger = logging.getLogger('health_checks')
health_logger.setLevel(getattr(logging, health_log_level))

# Add this after your logging configuration
class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        # Filter out health check logs
        return 'Health check at' not in record.getMessage()

# Apply the filter to the root logger
logging.getLogger().addFilter(HealthCheckFilter())

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Initialize OpenAI and Twilio clients
openai.api_key = os.getenv('OPENAI_API_KEY')
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

# Message retry queue
message_queue = queue.Queue()
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# Defina o fuso horário do Brasil
BRAZIL_TIMEZONE = pytz.timezone('America/Sao_Paulo')

# ===== RETRY MECHANISM =====

def message_sender_worker():
    """Background worker that processes the message queue and handles retries"""
    while True:
        try:
            # Get message from queue (blocks until a message is available)
            logger.info("Message sender worker waiting for messages...")
            message_data = message_queue.get()
            
            if message_data is None:
                # None is used as a signal to stop the thread
                logger.info("Message sender worker received stop signal")
                break
                
            to_number = message_data['to']
            body = message_data['body']
            retry_count = message_data.get('retry_count', 0)
            message_sid = message_data.get('message_sid')
            
            logger.info(f"Processing message from queue: to={to_number}, retry_count={retry_count}")
            
            try:
                # If we have a message_sid, check its status first
                if message_sid:
                    logger.info(f"Checking status of previous message {message_sid}")
                    message = twilio_client.messages(message_sid).fetch()
                    logger.info(f"Previous message status: {message.status}")
                    if message.status in ['delivered', 'read']:
                        logger.info(f"Message {message_sid} already delivered, skipping retry")
                        message_queue.task_done()
                        continue
                
                # Send or resend the message
                logger.info(f"Sending message to {to_number}: {body[:30]}...")
                message = twilio_client.messages.create(
                    body=body,
                    from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
                    to=to_number
                )
                
                logger.info(f"Message sent successfully: {message.sid} (status: {message.status})")
                message_queue.task_done()
                
            except TwilioRestException as e:
                logger.error(f"Twilio error: {str(e)}")
                logger.error(f"Twilio error code: {e.code}, status: {e.status}")
                
                # Check if we should retry
                if retry_count < MAX_RETRIES:
                    # Increment retry count and put back in queue
                    message_data['retry_count'] = retry_count + 1
                    message_data['message_sid'] = message_sid
                    logger.info(f"Scheduling retry {retry_count + 1}/{MAX_RETRIES} in {RETRY_DELAY} seconds")
                    
                    # Wait before retrying
                    time_module.sleep(RETRY_DELAY)
                    message_queue.put(message_data)
                else:
                    logger.error(f"Failed to send message after {MAX_RETRIES} attempts")
                
                message_queue.task_done()
                
            except Exception as e:
                logger.error(f"Unexpected error in message sender: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")
                
                # Check if we should retry
                if retry_count < MAX_RETRIES:
                    # Increment retry count and put back in queue
                    message_data['retry_count'] = retry_count + 1
                    logger.info(f"Scheduling retry {retry_count + 1}/{MAX_RETRIES} in {RETRY_DELAY} seconds")
                    
                    # Wait before retrying
                    time_module.sleep(RETRY_DELAY)
                    message_queue.put(message_data)
                else:
                    logger.error(f"Failed to send message after {MAX_RETRIES} attempts")
                
                message_queue.task_done()
                
        except Exception as e:
            logger.error(f"Error in message sender worker: {str(e)}")
            # Continue the loop to process the next message

def start_message_sender():
    """Start the background message sender thread"""
    logger.info("Starting message sender worker thread...")
    message_sender_thread = threading.Thread(target=message_sender_worker, daemon=True)
    message_sender_thread.start()
    logger.info(f"Message sender worker thread started: {message_sender_thread.name}, is_alive={message_sender_thread.is_alive()}")
    return message_sender_thread

def send_whatsapp_message(to_number, body):
    """Send a WhatsApp message using Twilio"""
    try:
        # Format the to number if it doesn't already have the WhatsApp prefix
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"
        
        logger.info(f"Queueing message for sending to {to_number}")
        
        # Add message to the queue
        message_queue.put({
            'to': to_number,
            'body': body,
            'retry_count': 0
        })
        
        logger.info(f"Message queued for sending to {to_number}")
        return True
    except Exception as e:
        logger.error(f"Error queueing message: {str(e)}")
        return False

# Initialize the ReminderAgent after send_whatsapp_message is defined
reminder_agent = ReminderAgent(send_message_func=send_whatsapp_message)

# ===== EXISTING FUNCTIONS =====

def ping_self():
    app_url = os.getenv('APP_URL', 'https://secretaria-app.onrender.com')
    
    while True:
        try:
            requests.get(app_url, timeout=5)
            logger.info(f"Self-ping successful")
        except Exception as e:
            logger.error(f"Self-ping failed: {str(e)}")
        
        # Sleep for 10 minutes (600 seconds)
        time_module.sleep(600)

# Start the self-ping in a background thread
def start_self_ping():
    ping_thread = threading.Thread(target=ping_self, daemon=True)
    ping_thread.start()
    logger.info("Self-ping background thread started")

def get_ai_response(message, is_audio_transcription=False):
    try:
        system_message = "You are a helpful WhatsApp assistant. Be concise and friendly in your responses."
        
        # Adicionar contexto sobre capacidade de áudio se for uma transcrição
        if is_audio_transcription:
            system_message += " You can process voice messages through transcription. The following message was received as an audio and transcribed to text."
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": message}
            ],
            max_tokens=150
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI API Error: {str(e)}")
        return "Desculpe, estou com dificuldades para processar sua solicitação no momento."

# ===== FUNÇÕES DE LEMBRETES =====

# ===== FIM DAS FUNÇÕES DE LEMBRETES =====

def process_image(image_url):
    """Process an image using OpenAI's vision model"""
    try:
        logger.info(f"Processing image: {image_url}")
        
        # Download the image WITH AUTHENTICATION
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        image_response = requests.get(image_url, auth=auth)
        
        if image_response.status_code != 200:
            raise Exception(f"Failed to download image: {image_response.status_code}")
        
        # Convert to base64 for OpenAI API
        image_base64 = base64.b64encode(image_response.content).decode('utf-8')
        
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that can analyze images. Be concise and friendly in your responses."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image_url", 
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        return "Desculpe, não consegui analisar esta imagem."

def transcribe_audio(audio_url):
    """Transcribe audio using OpenAI's Whisper API"""
    try:
        logger.info(f"Transcribing audio: {audio_url}")
        
        # Download the audio file WITH AUTHENTICATION
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        audio_response = requests.get(audio_url, auth=auth)
        
        if audio_response.status_code != 200:
            raise Exception(f"Failed to download audio: {audio_response.status_code}")
        
        # Save to a temporary file
        temp_file = "temp_audio.ogg"
        with open(temp_file, "wb") as f:
            f.write(audio_response.content)
        
        # Transcribe using OpenAI
        with open(temp_file, "rb") as audio_file:
            transcription = openai.Audio.transcribe(
                "whisper-1", 
                audio_file
            )
        
        # Clean up
        os.remove(temp_file)
        
        transcribed_text = transcription.text
        logger.info(f"Transcription result: {transcribed_text}")
        
        return transcribed_text
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}")
        return "Não foi possível transcrever o áudio."

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Twilio WhatsApp messages"""
    try:
        # Extract message details
        from_number = request.values.get('From', '')
        body = request.values.get('Body', '')
        num_media = int(request.values.get('NumMedia', 0))
        
        # Log the incoming message
        logger.info(f"Received message from {from_number}: {body[:50]}..." if len(body) > 50 else f"Received message from {from_number}: {body}")
        
        # Extract the phone number without the "whatsapp:" prefix
        user_phone = from_number.replace('whatsapp:', '')
        
        # Store the incoming message immediately
        if num_media > 0:
            media_type = request.values.get('MediaContentType0', '')
            media_url = request.values.get('MediaUrl0', '')
            store_conversation(user_phone, media_url, media_type.split('/')[0], True)
        else:
            store_conversation(user_phone, body, 'text', True)
        
        # Send an immediate acknowledgment response
        resp = MessagingResponse()
        
        # Start processing in a background thread
        threading.Thread(
            target=process_message_async,
            args=(from_number, body, num_media, request.values),
            daemon=True
        ).start()
        
        # For media messages, acknowledge receipt
        if num_media > 0:
            resp.message("Recebi sua mídia! Estou processando...")
        
        return str(resp)
    
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        resp = MessagingResponse()
        resp.message("Desculpe, ocorreu um erro ao processar sua mensagem.")
        return str(resp)

# ===== DIRECT MESSAGE ENDPOINT =====

@app.route('/send_message', methods=['POST'])
def send_direct_message():
    """Endpoint to send messages outside of the webhook context"""
    try:
        data = request.json
        to_number = data.get('to')
        body = data.get('body')
        
        if not to_number or not body:
            return {"error": "Missing 'to' or 'body' parameters"}, 400
            
        # Queue the message with our retry mechanism
        send_whatsapp_message(to_number, body)
        
        return {"status": "queued"}, 200
    except Exception as e:
        logger.error(f"Error in send_message endpoint: {str(e)}")
        return {"error": str(e)}, 500

def process_message_async(from_number, body, num_media, form_values):
    """Process a message asynchronously after sending an acknowledgment"""
    try:
        # Extract the phone number without the "whatsapp:" prefix
        user_phone = from_number.replace('whatsapp:', '')
        
        response_text = None
        
        # Process the message
        if num_media > 0:
            # Handle media messages
            media_urls = []
            for i in range(num_media):
                media_url = form_values.get(f'MediaUrl{i}')
                media_type = form_values.get(f'MediaContentType{i}')
                
                if media_url:
                    media_urls.append((media_url, media_type))
            
            # Process media
            media_type = form_values.get('MediaContentType0', '')
            
            if media_type.startswith('image/'):
                # Process image
                logger.info(f"Processing image from {from_number}")
                response_text = process_image(media_urls[0][0])
            
            elif media_type.startswith('audio/'):
                # Process audio
                logger.info(f"Processing audio from {from_number}")
                transcribed_text = transcribe_audio(media_urls[0][0])
                
                # Check for reminder intent in transcription
                is_reminder, intent = reminder_agent.detect_reminder_intent(transcribed_text)
                
                if is_reminder:
                    response_text = reminder_agent.handle_reminder_intent(user_phone, transcribed_text)
                else:
                    response_text = get_ai_response(transcribed_text, is_audio_transcription=True)
        else:
            # Check for reminder intent
            is_reminder, intent = reminder_agent.detect_reminder_intent(body)
            
            if is_reminder:
                # Handle reminder intent
                logger.info(f"Reminder intent detected: {intent}")
                response_text = reminder_agent.handle_reminder_intent(user_phone, body)
            else:
                # Handle general conversation
                logger.info("No reminder intent detected, handling as general conversation")
                response_text = handle_message(user_phone, body)
        
        # Send the response if we have one
        if response_text:
            # Store the response
            store_conversation(user_phone, response_text, 'text', False)
            
            # Send the message
            send_whatsapp_message(from_number, response_text)
            
    except Exception as e:
        logger.error(f"Error in async message processing: {str(e)}")
        error_message = "Desculpe, ocorreu um erro ao processar sua mensagem."
        send_whatsapp_message(from_number, error_message)

# Add this to your startup code
try:
    logger.info("Checking Twilio credentials...")
    account = twilio_client.api.accounts(os.getenv('TWILIO_ACCOUNT_SID')).fetch()
    logger.info(f"Twilio account status: {account.status}")
    
    # Try to list messages to verify API access
    messages = twilio_client.messages.list(limit=1)
    logger.info(f"Successfully retrieved {len(messages)} messages from Twilio")
except Exception as e:
    logger.error(f"Error checking Twilio credentials: {str(e)}")

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    # Use the health logger instead of the main logger
    health_logger.debug(f"Health check at {datetime.now().isoformat()}")
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    # Start the message sender thread
    app.message_sender_thread = start_message_sender()
    
    # Start the reminder checker thread
    reminder_checker_thread = reminder_agent.start_reminder_checker()
    
    # Start the self-ping thread if needed
    if os.getenv('ENABLE_SELF_PING', 'false').lower() == 'true':
        start_self_ping()
    
    # Start the Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)