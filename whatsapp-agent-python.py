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
from supabase import create_client
import json
from datetime import datetime, timezone, timedelta
import re

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Initialize OpenAI and Twilio clients
openai.api_key = os.getenv('OPENAI_API_KEY')
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

# Initialize Supabase client
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')  # Use service role key to bypass RLS
supabase = create_client(supabase_url, supabase_key)

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

def store_conversation(user_phone, message_content, message_type, is_from_user, agent="DEFAULT"):
    """Store a message in the Supabase conversations table"""
    try:
        data = {
            'user_phone': user_phone,
            'message_content': message_content,
            'message_type': message_type,
            'is_from_user': is_from_user,
            'agent': agent
        }
        
        result = supabase.table('conversations').insert(data).execute()
        print(f"Message stored in database: {message_type} from {'user' if is_from_user else 'agent'}")
        return True
    except Exception as e:
        print(f"Error storing message in database: {str(e)}")
        return False

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
        print(f"OpenAI API Error: {str(e)}")
        return "Desculpe, estou com dificuldades para processar sua solicitação no momento."

# ===== FUNÇÕES DE LEMBRETES =====

def detect_reminder_intent(message):
    """Detecta se a mensagem contém uma intenção de gerenciar lembretes"""
    # Palavras-chave em português para detecção rápida
    keywords = ["lembr", "alarm", "avis", "notific", "alert", "remind"]
    list_keywords = ["meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes"]
    cancel_keywords = ["cancelar lembrete", "apagar lembrete", "remover lembrete", "deletar lembrete"]
    
    message_lower = message.lower()
    
    # Verificação de listagem de lembretes
    for keyword in list_keywords:
        if keyword in message_lower:
            return True, "listar"
    
    # Verificação de cancelamento de lembretes
    for keyword in cancel_keywords:
        if keyword in message_lower:
            return True, "cancelar"
    
    # Verificação de criação de lembretes
    for keyword in keywords:
        if keyword in message_lower:
            return True, "criar"
            
    return False, None

def parse_reminder(message, action):
    """Extrai detalhes do lembrete usando GPT-4o-mini"""
    try:
        system_prompt = ""
        
        if action == "criar":
            system_prompt = """
            Extraia as informações de lembrete da mensagem do usuário. 
            Retorne um JSON com os seguintes campos:
            - title: o título ou assunto do lembrete
            - date: a data do lembrete (formato YYYY-MM-DD, ou 'hoje', 'amanhã')
            - time: a hora do lembrete (formato HH:MM)
            
            Se alguma informação estiver faltando, use null para o campo.
            """
        elif action == "cancelar":
            system_prompt = """
            Extraia as palavras-chave que identificam qual lembrete o usuário deseja cancelar.
            Retorne um JSON com o campo:
            - keywords: array de palavras-chave que identificam o lembrete
            """
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            response_format={"type": "json_object"}
        )
        
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Error parsing reminder: {str(e)}")
        return None

def parse_datetime(date_str, time_str):
    """Converte strings de data e hora para um objeto datetime"""
    try:
        now = datetime.now()
        
        # Processar a data
        if date_str.lower() == 'hoje':
            date = now.date()
        elif date_str.lower() == 'amanhã' or date_str.lower() == 'amanha':
            date = (now + timedelta(days=1)).date()
        else:
            # Tentar converter a string de data
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
        
        # Processar a hora
        if time_str:
            time_parts = time_str.split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            
            # Criar o datetime combinado
            dt = datetime.combine(date, datetime.min.time().replace(hour=hour, minute=minute))
            
            # Adicionar timezone
            dt = dt.replace(tzinfo=timezone.utc)
            return dt
        else:
            # Se não houver hora, usar meio-dia
            dt = datetime.combine(date, datetime.min.time().replace(hour=12, minute=0))
            dt = dt.replace(tzinfo=timezone.utc)
            return dt
            
    except Exception as e:
        print(f"Error parsing datetime: {str(e)}")
        # Retornar data/hora padrão (amanhã ao meio-dia)
        tomorrow_noon = (datetime.now() + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )
        return tomorrow_noon

def format_datetime(dt):
    """Formata um objeto datetime para exibição amigável"""
    # Converter para o fuso horário local se necessário
    if dt.tzinfo == timezone.utc:
        # Ajustar para o fuso horário do Brasil (UTC-3)
        dt = dt.astimezone(timezone(timedelta(hours=-3)))
    
    # Formatar a data
    today = datetime.now(dt.tzinfo).date()
    tomorrow = (datetime.now(dt.tzinfo) + timedelta(days=1)).date()
    
    if dt.date() == today:
        date_str = "hoje"
    elif dt.date() == tomorrow:
        date_str = "amanhã"
    else:
        # Formatar a data em português
        date_str = dt.strftime("%d/%m/%Y")
    
    # Formatar a hora
    time_str = dt.strftime("%H:%M")
    
    return f"{date_str} às {time_str}"

def create_reminder(user_phone, title, scheduled_time):
    """Cria um novo lembrete no Supabase"""
    try:
        data = {
            'user_phone': user_phone,
            'title': title,
            'scheduled_time': scheduled_time.isoformat(),
            'is_active': True
        }
        
        result = supabase.table('reminders').insert(data).execute()
        print(f"Reminder created: {title} at {scheduled_time}")
        return result.data[0]['id'] if result.data else None
    except Exception as e:
        print(f"Error creating reminder: {str(e)}")
        return None

def list_reminders(user_phone):
    """Lista todos os lembretes ativos do usuário"""
    try:
        result = supabase.table('reminders') \
            .select('*') \
            .eq('user_phone', user_phone) \
            .eq('is_active', True) \
            .order('scheduled_time') \
            .execute()
        
        return result.data
    except Exception as e:
        print(f"Error listing reminders: {str(e)}")
        return []

def format_reminder_list(reminders):
    """Formata a lista de lembretes para exibição"""
    if not reminders:
        return "Você não tem lembretes ativos no momento."
    
    result = "📋 *Seus lembretes:*\n\n"
    for i, reminder in enumerate(reminders, 1):
        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
        formatted_time = format_datetime(scheduled_time)
        result += f"{i}. *{reminder['title']}* - {formatted_time}\n"
    
    result += "\nPara cancelar um lembrete, envie 'cancelar lembrete [título]'"
    return result

def cancel_reminder(user_phone, keywords):
    """Cancela um lembrete baseado em palavras-chave do título"""
    try:
        # Primeiro, busca todos os lembretes ativos
        reminders = list_reminders(user_phone)
        
        if not reminders:
            return None
        
        # Encontra o lembrete mais provável
        best_match = None
        for reminder in reminders:
            title_lower = reminder['title'].lower()
            # Verifica se todas as palavras-chave estão no título
            if all(keyword.lower() in title_lower for keyword in keywords):
                best_match = reminder
                break
        
        if best_match:
            # Desativa o lembrete
            result = supabase.table('reminders') \
                .update({'is_active': False}) \
                .eq('id', best_match['id']) \
                .execute()
            
            print(f"Reminder cancelled: {best_match['title']}")
            return best_match
        
        return None
    except Exception as e:
        print(f"Error cancelling reminder: {str(e)}")
        return None

def check_and_send_reminders():
    """Verifica e envia lembretes programados para o momento atual"""
    try:
        # Obtém a hora atual
        now = datetime.now(timezone.utc)
        # Margem de 1 minuto para garantir que não perca nenhum lembrete
        time_window = now - timedelta(minutes=1)
        
        # Busca lembretes ativos programados para agora ou no passado recente
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .lte('scheduled_time', now.isoformat()) \
            .gt('scheduled_time', time_window.isoformat()) \
            .execute()
        
        reminders = result.data
        print(f"Found {len(reminders)} reminders to send")
        
        for reminder in reminders:
            # Envia a notificação via Twilio
            send_reminder_notification(reminder)
            
            # Atualiza o lembrete
            supabase.table('reminders') \
                .update({
                    'last_notification': now.isoformat(),
                    'is_active': False  # Desativa após enviar
                }) \
                .eq('id', reminder['id']) \
                .execute()
    except Exception as e:
        print(f"Error checking reminders: {str(e)}")

def send_reminder_notification(reminder):
    """Envia uma notificação de lembrete via Twilio"""
    try:
        user_phone = reminder['user_phone']
        message = f"🔔 *LEMBRETE*: {reminder['title']}"
        
        # Formata a mensagem do Twilio
        twilio_client.messages.create(
            body=message,
            from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
            to=f"whatsapp:{user_phone}"
        )
        
        print(f"Reminder notification sent to {user_phone}: {reminder['title']}")
    except Exception as e:
        print(f"Error sending reminder notification: {str(e)}")

def start_reminder_checker():
    """Inicia o verificador de lembretes em uma thread separada"""
    def reminder_checker_thread():
        while True:
            try:
                check_and_send_reminders()
            except Exception as e:
                print(f"Error in reminder checker thread: {str(e)}")
            
            # Verifica a cada minuto
            time.sleep(60)
    
    thread = threading.Thread(target=reminder_checker_thread, daemon=True)
    thread.start()
    print("Reminder checker thread started")

# ===== FIM DAS FUNÇÕES DE LEMBRETES =====

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
        # Extract the phone number without the "whatsapp:" prefix
        user_phone = sender_number.replace('whatsapp:', '')
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
            
            # Store the user's image message
            store_conversation(
                user_phone=user_phone,
                message_content=image_url,  # Store the URL of the image
                message_type='image',
                is_from_user=True
            )
            
            # Process the image with GPT-4o
            full_response = process_image(image_url)
            
            # Store the agent's response
            store_conversation(
                user_phone=user_phone,
                message_content=full_response,
                message_type='text',
                is_from_user=False
            )
            
        # Check if this is a voice message
        elif num_media > 0 and request.values.get('MediaContentType0', '').startswith('audio/'):
            print("Voice message detected")
            audio_url = request.values.get('MediaUrl0', '')
            print(f"Audio URL: {audio_url}")
            
            # Transcribe the audio
            transcribed_text = transcribe_audio(audio_url)
            print(f"Transcription: {transcribed_text}")
            
            # Store the user's transcribed audio message
            store_conversation(
                user_phone=user_phone,
                message_content=transcribed_text,  # Store the transcription instead of URL
                message_type='audio',
                is_from_user=True
            )
            
            # Check if this is a reminder intent
            is_reminder, action = detect_reminder_intent(transcribed_text)
            
            if is_reminder:
                print(f"Reminder intent detected: {action}")
                
                if action == "listar":
                    # Listar lembretes
                    reminders = list_reminders(user_phone)
                    full_response = format_reminder_list(reminders)
                
                elif action == "cancelar":
                    # Cancelar lembrete
                    reminder_data = parse_reminder(transcribed_text, action)
                    if reminder_data and 'keywords' in reminder_data:
                        cancelled = cancel_reminder(user_phone, reminder_data['keywords'])
                        if cancelled:
                            full_response = f"✅ Lembrete cancelado: {cancelled['title']}"
                        else:
                            full_response = "❌ Não encontrei nenhum lembrete com essa descrição."
                    else:
                        full_response = "❌ Não consegui identificar qual lembrete você deseja cancelar."
                
                elif action == "criar":
                    # Criar lembrete
                    reminder_data = parse_reminder(transcribed_text, action)
                    if reminder_data and 'title' in reminder_data and 'date' in reminder_data:
                        # Converter data/hora para timestamp
                        scheduled_time = parse_datetime(
                            reminder_data.get('date', 'amanhã'), 
                            reminder_data.get('time', '12:00')
                        )
                        
                        # Criar o lembrete
                        reminder_id = create_reminder(user_phone, reminder_data['title'], scheduled_time)
                        
                        if reminder_id:
                            full_response = f"✅ Lembrete criado: {reminder_data['title']} para {format_datetime(scheduled_time)}"
                        else:
                            full_response = "❌ Não consegui criar o lembrete. Por favor, tente novamente."
                    else:
                        full_response = "❌ Não consegui entender os detalhes do lembrete. Por favor, especifique o título e quando deseja ser lembrado."
            else:
                # Get AI response based on transcription
                full_response = get_ai_response(transcribed_text, is_audio_transcription=True)
            
            # Store the agent's response
            store_conversation(
                user_phone=user_phone,
                message_content=full_response,
                message_type='text',
                is_from_user=False
            )
            
        else:
            # Handle regular text message
            incoming_msg = request.values.get('Body', '')
            print(f"Text message: {incoming_msg}")
            
            # Store the user's text message
            store_conversation(
                user_phone=user_phone,
                message_content=incoming_msg,
                message_type='text',
                is_from_user=True
            )
            
            # Check if this is a reminder intent
            is_reminder, action = detect_reminder_intent(incoming_msg)
            
            if is_reminder:
                print(f"Reminder intent detected: {action}")
                
                if action == "listar":
                    # Listar lembretes
                    reminders = list_reminders(user_phone)
                    full_response = format_reminder_list(reminders)
                
                elif action == "cancelar":
                    # Cancelar lembrete
                    reminder_data = parse_reminder(incoming_msg, action)
                    if reminder_data and 'keywords' in reminder_data:
                        cancelled = cancel_reminder(user_phone, reminder_data['keywords'])
                        if cancelled:
                            full_response = f"✅ Lembrete cancelado: {cancelled['title']}"
                        else:
                            full_response = "❌ Não encontrei nenhum lembrete com essa descrição."
                    else:
                        full_response = "❌ Não consegui identificar qual lembrete você deseja cancelar."
                
                elif action == "criar":
                    # Criar lembrete
                    reminder_data = parse_reminder(incoming_msg, action)
                    if reminder_data and 'title' in reminder_data and 'date' in reminder_data:
                        # Converter data/hora para timestamp
                        scheduled_time = parse_datetime(
                            reminder_data.get('date', 'amanhã'), 
                            reminder_data.get('time', '12:00')
                        )
                        
                        # Criar o lembrete
                        reminder_id = create_reminder(user_phone, reminder_data['title'], scheduled_time)
                        
                        if reminder_id:
                            full_response = f"✅ Lembrete criado: {reminder_data['title']} para {format_datetime(scheduled_time)}"
                        else:
                            full_response = "❌ Não consegui criar o lembrete. Por favor, tente novamente."
                    else:
                        full_response = "❌ Não consegui entender os detalhes do lembrete. Por favor, especifique o título e quando deseja ser lembrado."
            else:
                # Get AI response for regular message
                full_response = get_ai_response(incoming_msg)
            
            # Store the agent's response
            store_conversation(
                user_phone=user_phone,
                message_content=full_response,
                message_type='text',
                is_from_user=False
            )
        
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
    print("WhatsApp AI Assistant is ready to respond to text, voice messages, and manage reminders!")
    start_self_ping()
    
    # Start the reminder checker
    start_reminder_checker()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)