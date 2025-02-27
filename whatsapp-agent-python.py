from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.base.exceptions import TwilioRestException
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
import queue
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

# Message retry queue
message_queue = queue.Queue()
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# ===== RETRY MECHANISM =====

def message_sender_worker():
    """Background worker that processes the message queue and handles retries"""
    while True:
        try:
            # Get message from queue (blocks until a message is available)
            message_data = message_queue.get()
            
            if message_data is None:
                # None is used as a signal to stop the thread
                break
                
            to_number = message_data['to']
            body = message_data['body']
            retry_count = message_data.get('retry_count', 0)
            message_sid = message_data.get('message_sid')
            
            try:
                # If we have a message_sid, check its status first
                if message_sid:
                    message = twilio_client.messages(message_sid).fetch()
                    if message.status in ['delivered', 'read']:
                        logger.info(f"Message {message_sid} already delivered, skipping retry")
                        message_queue.task_done()
                        continue
                
                # Send or resend the message
                message = twilio_client.messages.create(
                    body=body,
                    from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
                    to=to_number
                )
                
                logger.info(f"Message sent successfully: {message.sid}")
                message_queue.task_done()
                
            except TwilioRestException as e:
                logger.error(f"Twilio error: {str(e)}")
                
                # Check if we should retry
                if retry_count < MAX_RETRIES:
                    # Increment retry count and put back in queue
                    message_data['retry_count'] = retry_count + 1
                    message_data['message_sid'] = message_sid
                    logger.info(f"Scheduling retry {retry_count + 1}/{MAX_RETRIES} in {RETRY_DELAY} seconds")
                    
                    # Wait before retrying
                    time.sleep(RETRY_DELAY)
                    message_queue.put(message_data)
                else:
                    logger.error(f"Failed to send message after {MAX_RETRIES} attempts")
                
                message_queue.task_done()
                
            except Exception as e:
                logger.error(f"Unexpected error in message sender: {str(e)}")
                message_queue.task_done()
                
        except Exception as e:
            logger.error(f"Error in message sender worker: {str(e)}")

def start_message_sender():
    """Start the background message sender thread"""
    sender_thread = threading.Thread(target=message_sender_worker, daemon=True)
    sender_thread.start()
    logger.info("Message sender background thread started")
    return sender_thread

def send_whatsapp_message(to_number, body):
    """Queue a message to be sent with retry capability"""
    # Make sure the number has the whatsapp: prefix
    if not to_number.startswith('whatsapp:'):
        to_number = f"whatsapp:{to_number}"
    
    # Add message to the retry queue
    message_data = {
        'to': to_number,
        'body': body,
        'retry_count': 0
    }
    message_queue.put(message_data)
    logger.info(f"Message queued for sending to {to_number}")

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
        time.sleep(600)

# Start the self-ping in a background thread
def start_self_ping():
    ping_thread = threading.Thread(target=ping_self, daemon=True)
    ping_thread.start()
    logger.info("Self-ping background thread started")

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
        logger.info(f"Message stored in database: {message_type} from {'user' if is_from_user else 'agent'}")
        return True
    except Exception as e:
        logger.error(f"Error storing message in database: {str(e)}")
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
        logger.error(f"OpenAI API Error: {str(e)}")
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
              Se for um tempo relativo como "daqui 5 minutos", coloque a expressão completa aqui.
            - time: a hora do lembrete (formato HH:MM)
              Deixe vazio se o tempo estiver incluído na expressão de data relativa.
            
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
        logger.error(f"Error parsing reminder: {str(e)}")
        return None
    
def parse_datetime(date_str, time_str):
    """Converte strings de data e hora para um objeto datetime"""
    try:
        now = datetime.now(timezone.utc)
        
        # Verificar se temos valores nulos
        if date_str is None:
            # Se não tiver data, assumir hoje
            date = now.date()
        elif date_str.lower() == 'hoje':
            date = now.date()
        elif date_str.lower() == 'amanhã' or date_str.lower() == 'amanha':
            date = (now + timedelta(days=1)).date()
        else:
            # Tentar interpretar expressões relativas como "daqui X minutos/horas/dias"
            relative_match = re.search(r'daqui\s+(\d+)\s+(minutos?|horas?|dias?)', date_str.lower())
            if relative_match:
                amount = int(relative_match.group(1))
                unit = relative_match.group(2)
                
                if 'minuto' in unit:
                    return now + timedelta(minutes=amount)
                elif 'hora' in unit:
                    return now + timedelta(hours=amount)
                elif 'dia' in unit:
                    return now + timedelta(days=amount)
            
            # Se não for expressão relativa, tentar como data específica
            try:
                # Tentar formato YYYY-MM-DD
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                # Tentar formato DD/MM/YYYY
                try:
                    date = datetime.strptime(date_str, "%d/%m/%Y").date()
                except ValueError:
                    # Se falhar, usar amanhã como padrão
                    date = (now + timedelta(days=1)).date()
        
        # Se já retornamos um datetime completo (caso de tempo relativo)
        if isinstance(date, datetime):
            return date
        
        # Processar a hora se fornecida
        if time_str:
            # Verificar formato HH:MM
            if ':' in time_str:
                time_parts = time_str.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            else:
                # Tentar interpretar como apenas horas
                try:
                    hour = int(time_str)
                    minute = 0
                except ValueError:
                    # Hora padrão: meio-dia
                    hour = 12
                    minute = 0
            
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
        logger.error(f"Error parsing datetime: {str(e)}")
        # Retornar data/hora padrão (amanhã ao meio-dia)
        tomorrow_noon = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
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
        logger.info(f"Reminder created: {title} at {scheduled_time}")
        return result.data[0]['id'] if result.data else None
    except Exception as e:
        logger.error(f"Error creating reminder: {str(e)}")
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
        logger.error(f"Error listing reminders: {str(e)}")
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
            
            logger.info(f"Reminder cancelled: {best_match['title']}")
            return best_match
        
        return None
    except Exception as e:
        logger.error(f"Error cancelling reminder: {str(e)}")
        return None

def check_and_send_reminders():
    """Verifica e envia lembretes programados para o momento atual"""
    try:
        # Estatísticas para retornar
        stats = {
            "processed": 0,
            "sent": 0,
            "errors": 0,
            "late_processed": 0,
            "late_sent": 0
        }
        
        # Obtém a hora atual em UTC
        now = datetime.now(timezone.utc)
        logger.info(f"Checking reminders at {now.isoformat()} UTC / {to_local_timezone(now).isoformat()} local time")
        
        # Margem de 2 minutos para garantir que não perca nenhum lembrete
        time_window_start = now - timedelta(minutes=2)
        
        # Busca lembretes ativos programados para agora ou no passado recente
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .lte('scheduled_time', now.isoformat()) \
            .gt('scheduled_time', time_window_start.isoformat()) \
            .execute()
        
        reminders = result.data
        logger.info(f"Found {len(reminders)} reminders to send")
        stats["processed"] = len(reminders)
        
        for reminder in reminders:
            try:
                # Converter o horário programado para exibição
                scheduled_time_utc = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                scheduled_time_local = to_local_timezone(scheduled_time_utc)
                
                logger.info(f"Processing reminder: {reminder['id']} - {reminder['title']}")
                logger.info(f"Scheduled for: {scheduled_time_utc.isoformat()} UTC / {scheduled_time_local.isoformat()} local")
                
                # Envia a notificação
                success = send_reminder_notification(reminder)
                
                if success:
                    # Atualiza o lembrete apenas se o envio foi bem-sucedido
                    update_result = supabase.table('reminders') \
                        .update({
                            'last_notification': now.isoformat(),
                            'is_active': False  # Desativa após enviar
                        }) \
                        .eq('id', reminder['id']) \
                        .execute()
                    
                    logger.info(f"Reminder {reminder['id']} marked as sent")
                    stats["sent"] += 1
                else:
                    logger.warning(f"Failed to send reminder {reminder['id']}, will try again later")
                    stats["errors"] += 1
            except Exception as e:
                logger.error(f"Error processing reminder {reminder['id']}: {str(e)}")
                stats["errors"] += 1
        
        # Verificar também lembretes antigos
        old_result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .lt('scheduled_time', time_window_start.isoformat()) \
            .is_('last_notification', 'null') \
            .execute()
        
        old_reminders = old_result.data
        if old_reminders:
            logger.warning(f"Found {len(old_reminders)} old reminders that were missed!")
            stats["late_processed"] = len(old_reminders)
            
            for reminder in old_reminders:
                try:
                    # Enviar com uma mensagem especial indicando atraso
                    reminder['is_late'] = True
                    success = send_reminder_notification(reminder)
                    
                    if success:
                        # Atualizar o status
                        supabase.table('reminders') \
                            .update({
                                'last_notification': now.isoformat(),
                                'is_active': False
                            }) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        logger.info(f"Late reminder {reminder['id']} processed")
                        stats["late_sent"] += 1
                    else:
                        logger.warning(f"Failed to send late reminder {reminder['id']}, will try again later")
                        stats["errors"] += 1
                except Exception as e:
                    logger.error(f"Error processing late reminder {reminder['id']}: {str(e)}")
                    stats["errors"] += 1
        
        return stats
    
    except Exception as e:
        logger.error(f"Error checking reminders: {str(e)}")
        return {"processed": 0, "sent": 0, "errors": 1, "error_message": str(e)}

def send_reminder_notification(reminder):
    """Envia uma notificação de lembrete via sistema de retry"""
    try:
        user_phone = reminder['user_phone']
        
        # Verificar se é um lembrete atrasado
        if reminder.get('is_late'):
            message = f"🔔 *LEMBRETE ATRASADO*: {reminder['title']}\n\n(Este lembrete estava programado para {format_datetime(datetime.fromisoformat(reminder['scheduled_time']))})"
        else:
            message = f"🔔 *LEMBRETE*: {reminder['title']}"
        
        logger.info(f"Sending reminder to {user_phone}: {message}")
        
        # Enviar diretamente via Twilio para debug
        direct_message = twilio_client.messages.create(
            body=message,
            from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
            to=f"whatsapp:{user_phone}"
        )
        
        logger.info(f"Reminder sent directly via Twilio: {direct_message.sid}")
        
        # Também usar o sistema de retry como backup
        send_whatsapp_message(user_phone, message)
        
        # Armazenar a notificação na tabela de conversas
        store_conversation(
            user_phone=user_phone,
            message_content=message,
            message_type='text',
            is_from_user=False,
            agent="REMINDER"
        )
        
        return True
    except Exception as e:
        logger.error(f"Error sending reminder notification: {str(e)}")
        return False

def start_reminder_checker():
    """Inicia o verificador de lembretes em uma thread separada como backup"""
    def reminder_checker_thread():
        logger.info("Backup reminder checker thread started")
        
        # Configurar o intervalo de verificação (mais longo, já que temos o cron-job.org)
        check_interval = 300  # 5 minutos
        
        while True:
            try:
                # Dormir primeiro
                time.sleep(check_interval)
                
                # Depois verificar os lembretes
                logger.info("Running backup reminder check")
                check_and_send_reminders()
                
            except Exception as e:
                logger.error(f"Error in backup reminder checker: {str(e)}")
    
    thread = threading.Thread(target=reminder_checker_thread, daemon=True)
    thread.start()
    logger.info("Backup reminder checker background thread started")
    return thread

# ===== FIM DAS FUNÇÕES DE LEMBRETES =====

def process_image(image_url):
    try:
        # Download the image
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        response = requests.get(image_url, auth=auth)
        
        if response.status_code != 200:
            logger.error(f"Failed to download image: {response.status_code}")
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
        logger.error(f"Image Processing Error: {str(e)}")
        return "Desculpe, tive um problema ao analisar sua imagem."

def transcribe_audio(audio_url):
    try:
        # Download the audio file
        auth = (os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        response = requests.get(audio_url, auth=auth)
        
        if response.status_code != 200:
            logger.error(f"Failed to download audio: {response.status_code}")
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
        logger.info(f"Transcribed: {transcribed_text}")
        return transcribed_text
    
    except Exception as e:
        logger.error(f"Transcription Error: {str(e)}")
        return "Tive dificuldades para entender sua mensagem de voz."

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender_number = request.values.get('From', '')
        # Extract the phone number without the "whatsapp:" prefix
        user_phone = sender_number.replace('whatsapp:', '')
        num_media = int(request.values.get('NumMedia', 0))
        
        # Debug logging
        logger.info(f"Incoming message from {sender_number} with {num_media} media attachments")
        
        # Check if this is an image
        if num_media > 0 and request.values.get('MediaContentType0', '').startswith('image/'):
            logger.info("Image message detected")
            image_url = request.values.get('MediaUrl0', '')
            logger.info(f"Image URL: {image_url}")
            
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
            logger.info("Voice message detected")
            audio_url = request.values.get('MediaUrl0', '')
            logger.info(f"Audio URL: {audio_url}")
            
            # Transcribe the audio
            transcribed_text = transcribe_audio(audio_url)
            logger.info(f"Transcription: {transcribed_text}")
            
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
                logger.info(f"Reminder intent detected: {action}")
                
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
            logger.info(f"Text message: {incoming_msg}")
            
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
                logger.info(f"Reminder intent detected: {action}")
                
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
        
        # When sending the response, use our retry mechanism instead of Twilio's TwiML
        # This is for asynchronous responses outside the webhook context
        
        # For webhook responses, we still use TwiML as it's more reliable in this context
        resp = MessagingResponse()
        resp.message(full_response)
        
        logger.info(f"Sending response via webhook: {full_response[:50]}...")
        return str(resp)

    except Exception as e:
        logger.error(f"Error in webhook: {type(e).__name__} - {str(e)}")
        
        # Return a friendly error message in Portuguese
        resp = MessagingResponse()
        resp.message("Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente mais tarde.")
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

@app.route('/api/check-reminders', methods=['POST', 'GET'])
def api_check_reminders():
    """Endpoint para verificação de lembretes via cron job externo"""
    try:
        # Verificar autenticação
        api_key = request.headers.get('X-API-Key')
        if api_key != os.getenv('REMINDER_API_KEY'):
            logger.warning(f"Unauthorized attempt to access check-reminders endpoint. IP: {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401
            
        # Registrar a chamada
        logger.info(f"Reminder check triggered via API. Time: {datetime.now(timezone.utc).isoformat()}")
        
        # Processar lembretes
        result = check_and_send_reminders()
        
        # Retornar resultado
        return jsonify({
            "status": "success",
            "processed": result.get("processed", 0),
            "sent": result.get("sent", 0),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        logger.error(f"Error in check-reminders endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return 'Assistente WhatsApp está funcionando!'

if __name__ == '__main__':
    # Start background threads
    logger.info("Starting WhatsApp Assistant")
    start_self_ping()
    start_reminder_checker()
    message_sender_thread = start_message_sender()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)