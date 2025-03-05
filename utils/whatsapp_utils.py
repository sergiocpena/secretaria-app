"""
WhatsApp utility functions.
This file provides functions for interacting with the WhatsApp API via Twilio.
"""
import logging
import os
import requests
import threading
import time as time_module
import queue
from flask import request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)

# Initialize Twilio client
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

# Message retry queue
message_queue = queue.Queue()
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

def message_sender_worker():
    """Background worker that processes the message queue and handles retries"""
    logger.info("Message sender worker started")
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
                logger.error(f"Twilio error sending message: {str(e)}")
                
                # Handle rate limiting
                if e.code == 20429:
                    logger.warning("Rate limit exceeded, will retry later")
                    # Increase retry count
                    message_data['retry_count'] = retry_count + 1
                    # Add back to queue if under max retries
                    if retry_count < MAX_RETRIES:
                        logger.info(f"Re-queueing message (retry {retry_count+1}/{MAX_RETRIES})")
                        time_module.sleep(RETRY_DELAY)
                        message_queue.put(message_data)
                    else:
                        logger.error(f"Max retries ({MAX_RETRIES}) reached, dropping message")
                else:
                    logger.error(f"Unhandled Twilio error: {e.code} - {e.msg}")
                
                message_queue.task_done()
            except Exception as e:
                logger.error(f"Error sending message: {str(e)}")
                message_queue.task_done()
                
        except Exception as e:
            logger.error(f"Error in message sender worker: {str(e)}")
            # Don't break the loop on error
            time_module.sleep(1)

def start_message_sender():
    """Start the background message sender thread"""
    logger.info("Starting message sender worker thread...")
    message_sender_thread = threading.Thread(target=message_sender_worker, daemon=True)
    message_sender_thread.start()
    return message_sender_thread

def send_whatsapp_message(to_number, body):
    """Send a WhatsApp message using Twilio"""
    try:
        # Ensure the number has the whatsapp: prefix
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'
            
        # Add the message to the queue
        logger.info(f"Queueing message to {to_number}")
        message_data = {
            'to': to_number,
            'body': body,
            'retry_count': 0
        }
        message_queue.put(message_data)
        return True
    except Exception as e:
        logger.error(f"Error queueing message: {str(e)}")
        return False

def download_media(media_url):
    """Download media from Twilio's servers"""
    try:
        logger.info(f"Downloading media from: {media_url}")
        
        # Download the media WITH AUTHENTICATION
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        media_response = requests.get(media_url, auth=auth)
        
        if media_response.status_code != 200:
            raise Exception(f"Failed to download media: {media_response.status_code}")
        
        return media_response.content
    except Exception as e:
        logger.error(f"Error downloading media: {str(e)}")
        return None

def parse_twilio_request(request):
    """Parse a Twilio webhook request and extract relevant data"""
    try:
        # Get form data from the request
        form_values = request.form.to_dict()
        
        # Extract key information
        from_number = form_values.get('From', '')
        body = form_values.get('Body', '')
        num_media = int(form_values.get('NumMedia', '0'))
        
        # Extract media information if present
        media_items = []
        if num_media > 0:
            for i in range(num_media):
                media_url = form_values.get(f'MediaUrl{i}')
                media_type = form_values.get(f'MediaContentType{i}')
                
                if media_url:
                    media_items.append({
                        'url': media_url,
                        'type': media_type
                    })
        
        # Return structured data
        return {
            'from_number': from_number,
            'body': body,
            'num_media': num_media,
            'media_items': media_items,
            'raw_form': form_values
        }
    except Exception as e:
        logger.error(f"Error parsing Twilio request: {str(e)}")
        # Return a minimal structure in case of error
        return {
            'from_number': '',
            'body': '',
            'num_media': 0,
            'media_items': [],
            'raw_form': {},
            'error': str(e)
        }

# New functions moved from whatsapp-agent-python.py

def webhook_handler(request, process_message_callback, intent_classifier=None, reminder_agent=None):
    """Webhook endpoint handler for Twilio WhatsApp messages"""
    try:
        # Parse the Twilio request
        twilio_data = parse_twilio_request(request)
        from_number = twilio_data['from_number']
        body = twilio_data['body']
        num_media = twilio_data['num_media']
        
        # Log the incoming message
        logger.info(f"Received message from {from_number}: {body[:50]}... (truncated)")
        
        # Send an acknowledgment response
        resp = MessagingResponse()
        
        # Process the message asynchronously
        threading.Thread(
            target=process_message_callback,
            args=(from_number, body, num_media, twilio_data['raw_form'])
        ).start()
        
        return str(resp)
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        return "Error", 500

def send_direct_message_handler(request):
    """Handler for sending messages outside of the webhook context"""
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

def process_message_async(from_number, body, num_media, form_values, intent_classifier, reminder_agent, handle_message, get_ai_response, process_image, transcribe_audio):
    """Process a message asynchronously after sending an acknowledgment"""
    try:
        # Extract the phone number without the "whatsapp:" prefix
        user_phone = from_number.replace('whatsapp:', '')
        
        response_text = None
        
        # Process the message
        if num_media > 0:
            # Handle media messages
            media_items = []
            for i in range(num_media):
                media_url = form_values.get(f'MediaUrl{i}')
                media_type = form_values.get(f'MediaContentType{i}')
                
                if media_url:
                    media_items.append((media_url, media_type))
            
            # Process media
            media_type = form_values.get('MediaContentType0', '')
            
            if media_type.startswith('image/'):
                # Process image
                logger.info(f"Processing image from {from_number}")
                response_text = process_image(media_items[0][0])
            
            elif media_type.startswith('audio/'):
                # Process audio
                logger.info(f"Processing audio from {from_number}")
                transcribed_text = transcribe_audio(media_items[0][0])
                
                # Check for intent in transcription
                intent_type, intent_details = intent_classifier.detect_intent(transcribed_text)
                
                if intent_type == "reminder":
                    response_text = reminder_agent.handle_reminder_intent(user_phone, transcribed_text)
                else:
                    response_text = get_ai_response(transcribed_text, is_audio_transcription=True)
        else:
            # Check for intent
            intent_type, intent_details = intent_classifier.detect_intent(body)
            
            if intent_type == "reminder":
                # Handle reminder intent
                logger.info(f"Reminder intent detected: {intent_details}")
                response_text = reminder_agent.handle_reminder_intent(user_phone, body)
            else:
                # Handle general conversation
                logger.info("No reminder intent detected, handling as general conversation")
                response_text = handle_message(user_phone, body)
        
        # Send the response
        if response_text:
            send_whatsapp_message(from_number, response_text)
        
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}")
        error_message = "Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente mais tarde."
        send_whatsapp_message(from_number, error_message) 