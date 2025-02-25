from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Initialize OpenAI and Twilio clients
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

def get_ai_response(message):
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful WhatsApp assistant. Be concise and friendly in your responses. When you get ambiguous messages, ask clarification questions."},
                {"role": "user", "content": message}
            ],
            max_tokens=150  # Limit response length
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"OpenAI API Error: {str(e)}")
        return "I apologize, but I'm having trouble processing your request right now."

def transcribe_audio(audio_url):
    try:
        # Download the audio file with proper authentication
        account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        
        print(f"Downloading audio with credentials - SID: {account_sid[:5]}... Token: {auth_token[:5]}...")
        
        # Use basic auth with Twilio credentials
        response = requests.get(
            audio_url, 
            auth=(account_sid, auth_token),
            headers={'Accept': 'application/octet-stream'}
        )
        
        print(f"Download status code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Failed to download audio: {response.status_code}")
            print(f"Response content: {response.text[:100]}")  # Print first 100 chars of response
            return "I couldn't process your voice message."
        
        # Save the audio file temporarily
        temp_file_path = "temp_audio.ogg"
        with open(temp_file_path, "wb") as f:
            f.write(response.content)
        
        print(f"Audio saved to {temp_file_path}, size: {len(response.content)} bytes")
        
        # Transcribe using OpenAI's Whisper API
        with open(temp_file_path, "rb") as audio_file:
            print("Sending to OpenAI for transcription...")
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        
        # Clean up the temporary file
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            print("Temporary audio file removed")
        
        transcribed_text = transcript.text
        print(f"Transcribed: {transcribed_text}")
        return transcribed_text
    
    except Exception as e:
        print(f"Transcription Error: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return "I had trouble understanding your voice message."

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender_number = request.values.get('From', '')
        num_media = int(request.values.get('NumMedia', 0))
        
        # Debug logging
        print("\n=== Incoming Message ===")
        print(f"From: {sender_number}")
        print(f"Media: {num_media}")
        
        # Check if this is a voice message
        if num_media > 0 and request.values.get('MediaContentType0', '').startswith('audio/'):
            print("Voice message detected")
            audio_url = request.values.get('MediaUrl0', '')
            print(f"Audio URL: {audio_url}")
            
            # Transcribe the audio
            transcribed_text = transcribe_audio(audio_url)
            print(f"Transcription: {transcribed_text}")
            
            # Get AI response based on transcription
            ai_response = get_ai_response(transcribed_text)
            response_prefix = "I heard: \"" + transcribed_text + "\"\n\nMy response: "
            full_response = response_prefix + ai_response
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
        return str(MessagingResponse())

@app.route('/', methods=['GET'])
def home():
    return 'WhatsApp AI Assistant is running!'

if __name__ == '__main__':
    print("\n=== Starting Server ===")
    print("WhatsApp AI Assistant is ready to respond to text and voice messages!")
    app.run(debug=True, port=5000)