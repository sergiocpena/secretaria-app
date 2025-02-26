from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
import requests
from dotenv import load_dotenv
import threading
import time
import base64

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Initialize OpenAI and Twilio clients
openai.api_key = os.getenv('OPENAI_API_KEY')
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

def ping_self():
    app_url = os.getenv('APP_URL', 'https://secretaria-app.onrender.com')
    
    while True:
        try:
            requests.get(app_url, timeout=5)
            print(f"Self-ping successful at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"Self-ping failed: {str(e)}")
        
        # Sleep for 10 minutes (600 seconds)
        time.sleep(600)

# Start the self-ping in a background thread
def start_self_ping():
    ping_thread = threading.Thread(target=ping_self, daemon=True)
    ping_thread.start()
    print("Self-ping background thread started")

def get_ai_response(message):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Upgraded from gpt-3.5-turbo
            messages=[
                {"role": "system", "content": "You are a helpful WhatsApp assistant. Be concise and friendly in your responses."},
                {"role": "user", "content": message}
            ],
            max_tokens=150  # Limit response length
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"OpenAI API Error: {str(e)}")
        return "Desculpe, estou com dificuldades para processar sua solicitação no momento."

def process_image(image_url):
    try:
        # Download the image
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        response = requests.get(image_url, auth=auth)
        
        if response.status_code != 200:
            print(f"Failed to download image: {response.status_code}")
            return "Não consegui baixar sua imagem."
            
        # Convert image to base64
        image_base64 = base64.b64encode(response.content).decode('utf-8')
        
        # Send to GPT-4o (which has vision capabilities)
        response = openai.ChatCompletion.create(
            model="gpt-4o",  # Updated from gpt-4-vision-preview to gpt-4o
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Descreva o que você vê nesta imagem em detalhes."},
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
        print(f"Image Processing Error: {str(e)}")
        return "Desculpe, tive um problema ao analisar sua imagem."

def transcribe_audio(audio_url):
    try:
        # Download the audio file
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        response = requests.get(audio_url, auth=auth)
        
        if response.status_code != 200:
            print(f"Failed to download audio: {response.status_code}")
            return "Não consegui processar sua mensagem de voz."
        
        # Save the audio file temporarily
        temp_file_path = "temp_audio.ogg"
        with open(temp_file_path, "wb") as f:
            f.write(response.content)
        
        # Transcribe using OpenAI's Whisper API
        with open(temp_file_path, "rb") as audio_file:
            transcript = openai.Audio.transcribe(
                "whisper-1",
                audio_file
            )
        
        # Clean up the temporary file
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        
        transcribed_text = transcript.text
        print(f"Transcribed: {transcribed_text}")
        return transcribed_text
    
    except Exception as e:
        print(f"Transcription Error: {str(e)}")
        return "Tive dificuldades para entender sua mensagem de voz."

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender_number = request.values.get('From', '')
        num_media = int(request.values.get('NumMedia', 0))
        
        # Debug logging
        print("\n=== Incoming Message ===")
        print(f"From: {sender_number}")
        print(f"Media: {num_media}")
        
        # Check if this is an image
        if num_media > 0 and request.values.get('MediaContentType0', '').startswith('image/'):
            print("Image message detected")
            image_url = request.values.get('MediaUrl0', '')
            print(f"Image URL: {image_url}")
            
            # Process the image with GPT-4o
            full_response = process_image(image_url)
            
        # Check if this is a voice message
        elif num_media > 0 and request.values.get('MediaContentType0', '').startswith('audio/'):
            print("Voice message detected")
            audio_url = request.values.get('MediaUrl0', '')
            print(f"Audio URL: {audio_url}")
            
            # Transcribe the audio
            transcribed_text = transcribe_audio(audio_url)
            print(f"Transcription: {transcribed_text}")
            
            # Get AI response based on transcription
            full_response = get_ai_response(transcribed_text)
            
        else:
            # Handle regular text message
            incoming_msg = request.values.get('Body', '')
            print(f"Text message: {incoming_msg}")
            full_response = get_ai_response(incoming_msg)
        
        # Create Twilio response
        resp = MessagingResponse()
        resp.message(full_response)
        
        print(f"\n=== Sending Response ===")
        print(f"Response: {full_response}")
        return str(resp)

    except Exception as e:
        print(f"\n=== Error ===")
        print(f"Type: {type(e).__name__}")
        print(f"Details: {str(e)}")
        
        # Return a friendly error message in Portuguese
        resp = MessagingResponse()
        resp.message("Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente mais tarde.")
        return str(resp)

@app.route('/', methods=['GET'])
def home():
    return 'Assistente WhatsApp está funcionando!'

if __name__ == '__main__':
    # Start the self-ping background thread
    print("\n=== Starting Server ===")
    print("WhatsApp AI Assistant is ready to respond to text and voice messages!")
    start_self_ping()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)