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
from supabase import create_client
import json
from datetime import datetime, timezone, timedelta, time
import re
import queue
import logging
import pytz

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

# Defina o fuso horário do Brasil
BRAZIL_TIMEZONE = pytz.timezone('America/Sao_Paulo')

def to_local_timezone(utc_dt):
    """Converte um datetime UTC para o fuso horário local (Brasil)"""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(BRAZIL_TIMEZONE)

def to_utc_timezone(local_dt):
    """Converte um datetime local para UTC"""
    if local_dt.tzinfo is None:
        # Assume que é horário local
        local_dt = BRAZIL_TIMEZONE.localize(local_dt)
    return local_dt.astimezone(timezone.utc)

def format_datetime(dt, to_local=True):
    """Formata um datetime para exibição amigável, convertendo para horário local se necessário"""
    if to_local:
        dt = to_local_timezone(dt)
    
    # Formatar a data em português
    weekdays = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    months = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    
    weekday = weekdays[dt.weekday()]
    month = months[dt.month - 1]
    
    # Verificar se é hoje ou amanhã
    now = datetime.now(dt.tzinfo)
    if dt.date() == now.date():
        date_str = "hoje"
    elif dt.date() == (now + timedelta(days=1)).date():
        date_str = "amanhã"
    else:
        date_str = f"{weekday}, {dt.day} de {month}"
    
    # Formatar a hora
    time_str = dt.strftime("%H:%M")
    
    return f"{date_str} às {time_str}"

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
                    time_module.sleep(RETRY_DELAY)
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
        time_module.sleep(600)

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
    message_lower = message.lower()
    
    # Verificação de listagem de lembretes
    list_keywords = ["meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes", "quais são meus lembretes"]
    for keyword in list_keywords:
        if keyword in message_lower:
            return True, "listar"
    
    # Verificação de cancelamento de lembretes
    cancel_keywords = ["cancelar lembrete", "apagar lembrete", "remover lembrete", "deletar lembrete", 
                       "excluir lembrete", "cancelar lembretes", "apagar lembretes", "remover lembretes", 
                       "deletar lembretes", "excluir lembretes"]
    for keyword in cancel_keywords:
        if keyword in message_lower:
            return True, "cancelar"
    
    # Verificação de criação de lembretes
    create_keywords = ["me lembra", "me lembre", "lembre-me", "criar lembrete", "novo lembrete", "adicionar lembrete"]
    for keyword in create_keywords:
        if keyword in message_lower:
            return True, "criar"
    
    # Se apenas a palavra "lembrete" ou "lembretes" estiver presente, perguntar o que o usuário deseja fazer
    if "lembrete" in message_lower or "lembretes" in message_lower:
        return True, "clarify"
            
    return False, None

def parse_reminder(message, action):
    """Extrai detalhes do lembrete usando GPT-4o-mini"""
    try:
        # Get current time in Brazil timezone
        brazil_tz = pytz.timezone('America/Sao_Paulo')
        now_local = datetime.now(brazil_tz)
        
        system_prompt = ""
        
        if action == "criar":
            system_prompt = f"""
            Você é um assistente especializado em extrair informações de lembretes de mensagens em português.
            
            A hora atual no Brasil é: {now_local.strftime('%Y-%m-%d %H:%M')} (UTC-3)
            
            Extraia as informações de lembrete da mensagem do usuário.
            Se houver múltiplos lembretes, retorne um array de objetos.
            
            Retorne um JSON com o seguinte formato:
            {{
              "reminders": [
                {{
                  "title": "título ou assunto do lembrete",
                  "datetime": {{
                    "year": ano (ex: 2025),
                    "month": mês (1-12),
                    "day": dia (1-31),
                    "hour": hora em formato 24h (0-23),
                    "minute": minuto (0-59)
                  }}
                }}
              ]
            }}
            
            Para tempos relativos como "daqui 5 minutos", "em 2 horas", "daqui 2h", calcule a data e hora exatas.
            Para expressões como "amanhã às 15h", determine a data e hora completas.
            Para horários sem data, use hoje se o horário ainda não passou, caso contrário use amanhã.
            """
        elif action == "cancelar":
            system_prompt = """
            Você é um assistente especializado em extrair informações sobre cancelamento de lembretes em português.
            
            Analise a mensagem do usuário e identifique quais lembretes ele deseja cancelar.
            
            Retorne um JSON com o seguinte formato:
            {
              "cancel_type": "number" ou "title" ou "range" ou "all",
              "numbers": [lista de números mencionados] (se cancel_type for "number" ou "range"),
              "range_start": número inicial (se cancel_type for "range"),
              "range_end": número final (se cancel_type for "range"),
              "title": "título ou palavras-chave do lembrete" (se cancel_type for "title")
            }
            
            Exemplos:
            - "cancelar lembrete 2" → {"cancel_type": "number", "numbers": [2]}
            - "cancelar lembretes 1, 3 e 5" → {"cancel_type": "number", "numbers": [1, 3, 5]}
            - "cancelar lembretes 1 a 3" → {"cancel_type": "range", "range_start": 1, "range_end": 3}
            - "cancelar os três primeiros lembretes" → {"cancel_type": "range", "range_start": 1, "range_end": 3}
            - "cancelar os 2 primeiros lembretes" → {"cancel_type": "range", "range_start": 1, "range_end": 2}
            - "cancelar lembrete reunião" → {"cancel_type": "title", "title": "reunião"}
            - "cancelar todos os lembretes" → {"cancel_type": "all"}
            - "excluir todos os lembretes" → {"cancel_type": "all"}
            - "apagar todos os lembretes" → {"cancel_type": "all"}
            """
        
        logger.info(f"Sending request to LLM for parsing {action} intent")
        start_time = time_module.time()
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            response_format={"type": "json_object"},
            temperature=0.1  # Lower temperature for more consistent parsing
        )
        elapsed_time = time_module.time() - start_time
        
        parsed_response = json.loads(response.choices[0].message.content)
        logger.info(f"Parsed {action} data: {parsed_response} (took {elapsed_time:.2f}s)")
        
        # Process the datetime components for each reminder
        if action == "criar" and "reminders" in parsed_response:
            for reminder in parsed_response["reminders"]:
                if "datetime" in reminder and isinstance(reminder["datetime"], dict):
                    dt_components = reminder["datetime"]
                    try:
                        # Create datetime object from components
                        dt = datetime(
                            year=dt_components.get('year', now_local.year),
                            month=dt_components.get('month', now_local.month),
                            day=dt_components.get('day', now_local.day),
                            hour=dt_components.get('hour', 12),
                            minute=dt_components.get('minute', 0),
                            second=0,
                            microsecond=0
                        )
                        
                        # Add timezone info (Brazil)
                        dt = brazil_tz.localize(dt)
                        
                        # Convert to UTC
                        dt_utc = dt.astimezone(timezone.utc)
                        
                        # Store the datetime object directly
                        reminder["scheduled_time"] = dt_utc
                        
                    except (KeyError, ValueError) as e:
                        logger.error(f"Error creating datetime from components: {str(e)}")
                        # Fall back to tomorrow at noon
                        tomorrow_noon = (now_local + timedelta(days=1)).replace(
                            hour=12, minute=0, second=0, microsecond=0
                        )
                        reminder["scheduled_time"] = tomorrow_noon.astimezone(timezone.utc)
        
        return parsed_response
    except Exception as e:
        logger.error(f"Error parsing reminder: {str(e)}")
        return None

def parse_datetime_with_llm(date_str):
    """Uses the LLM to parse natural language date/time expressions"""
    try:
        # Get current time in Brazil timezone
        brazil_tz = pytz.timezone('America/Sao_Paulo')
        now_local = datetime.now(brazil_tz)
        
        logger.info(f"Parsing datetime expression with LLM: '{date_str}'")
        
        # If it's already a datetime object, just return it
        if isinstance(date_str, datetime):
            if date_str.tzinfo is None:
                date_str = brazil_tz.localize(date_str)
            return date_str.astimezone(timezone.utc)
            
        # If it's None or empty, return tomorrow at noon
        if not date_str:
            tomorrow_noon = (now_local + timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            return tomorrow_noon.astimezone(timezone.utc)
        
        # Use the LLM to parse the date/time expression
        system_prompt = f"""
        You are a datetime parsing assistant. Convert the given natural language time expression to a specific date and time.
        
        Current local time in Brazil: {now_local.strftime('%Y-%m-%d %H:%M')} (UTC-3)
        
        Return a JSON with these fields:
        - year: the year (e.g., 2025)
        - month: the month (1-12)
        - day: the day (1-31)
        - hour: the hour in 24-hour format (0-23)
        - minute: the minute (0-59)
        - relative: boolean indicating if this was a relative time expression
        
        For relative times like "daqui 5 minutos" or "em 2 horas", calculate the exact target time.
        For expressions like "amanhã às 15h", determine the complete date and time.
        For times without dates, use today if the time hasn't passed yet, otherwise use tomorrow.
        """
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": date_str}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        parsed = json.loads(response.choices[0].message.content)
        logger.info(f"LLM parsed datetime: {parsed}")
        
        # Create datetime object from parsed components
        try:
            dt = datetime(
                year=parsed['year'],
                month=parsed['month'],
                day=parsed['day'],
                hour=parsed['hour'],
                minute=parsed['minute'],
                second=0,
                microsecond=0
            )
            
            # Add timezone info (Brazil)
            dt = brazil_tz.localize(dt)
            
            # Convert to UTC
            dt_utc = dt.astimezone(timezone.utc)
            logger.info(f"Final datetime (UTC): {dt_utc}")
            
            return dt_utc
            
        except (KeyError, ValueError) as e:
            logger.error(f"Error creating datetime from LLM response: {str(e)}")
            # Fall back to tomorrow at noon
            tomorrow_noon = (now_local + timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            return tomorrow_noon.astimezone(timezone.utc)
            
    except Exception as e:
        logger.error(f"Error in parse_datetime_with_llm: {str(e)}")
        # Return default time (tomorrow at noon)
        brazil_tz = pytz.timezone('America/Sao_Paulo')
        now_local = datetime.now(brazil_tz)
        tomorrow_noon = (now_local + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        return tomorrow_noon.astimezone(timezone.utc)

def create_reminder(user_phone, title, scheduled_time):
    """Cria um novo lembrete no banco de dados"""
    try:
        logger.info(f"Creating reminder: title='{title}', time={scheduled_time}, user={user_phone}")
        
        # Verificar se o título está vazio
        if not title or title.strip() == "":
            logger.error("Cannot create reminder with empty title")
            return None
        
        # Verificar se a data/hora é válida
        if not scheduled_time:
            logger.error("Cannot create reminder with empty scheduled time")
            return None
        
        # Verificar se scheduled_time é um objeto datetime com timezone
        if not isinstance(scheduled_time, datetime) or scheduled_time.tzinfo is None:
            logger.error(f"Invalid scheduled_time format: {type(scheduled_time)}, tzinfo={getattr(scheduled_time, 'tzinfo', None)}")
            # Try to fix it if possible
            if isinstance(scheduled_time, datetime):
                scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
            else:
                return None
        
        # Inserir o lembrete no banco de dados - without retry_count field
        reminder_data = {
            'user_phone': user_phone,
            'title': title,
            'scheduled_time': scheduled_time.isoformat(),
            'is_active': True,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        
        # Now insert with the appropriate fields
        result = supabase.table('reminders').insert(reminder_data).execute()
        
        if result and result.data and len(result.data) > 0:
            reminder_id = result.data[0]['id']
            logger.info(f"Successfully created reminder with ID: {reminder_id}")
            return reminder_id
        else:
            logger.error(f"Failed to create reminder, unexpected response: {result}")
            return None
            
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
    
    response = "📋 *Seus lembretes:*\n"
    
    for i, reminder in enumerate(reminders, 1):
        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
        formatted_time = format_datetime(scheduled_time)
        response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
    
    response += "\nPara cancelar um lembrete, envie 'cancelar lembrete 2' (usando o número) ou 'cancelar lembrete [título]' (usando o nome)"
    
    return response

def handle_reminder_intent(user_phone, message_text):
    """Processa intenções relacionadas a lembretes"""
    try:
        # Normalizar o texto da mensagem
        if isinstance(message_text, (list, tuple)):
            message_text = ' '.join(str(x) for x in message_text)  # Convert list to string safely
        normalized_text = str(message_text).lower().strip()
        
        logger.info(f"Processing reminder intent with normalized text: '{normalized_text}'")
        
        # Use the same list_keywords as in detect_reminder_intent
        list_keywords = ["lembretes", "meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes"]
        if any(keyword in normalized_text for keyword in list_keywords):
            reminders = list_reminders(user_phone)
            return format_reminder_list(reminders)
            
        # Verificar se é uma solicitação para cancelar lembretes
        cancel_keywords = ["cancelar", "remover", "apagar", "deletar", "excluir"]
        is_cancel_request = any(keyword in normalized_text for keyword in cancel_keywords)
        
        if is_cancel_request:
            logger.info("Detected cancel reminder request")
            
            # First, get the list of active reminders for this user
            reminders = list_reminders(user_phone)
            logger.info(f"Found {len(reminders)} active reminders for user {user_phone}")
            
            if not reminders:
                return "Você não tem lembretes ativos para cancelar."
            
            # Special case for "cancelar todos os lembretes" - handle directly without parsing
            if "todos os lembretes" in normalized_text or "todos lembretes" in normalized_text:
                logger.info("Detected 'cancel all reminders' request directly from text")
                cancelled_reminders = []
                
                for reminder in reminders:
                    try:
                        logger.info(f"Attempting to cancel reminder {reminder['id']} (title: {reminder['title']})")
                        update_result = supabase.table('reminders') \
                            .update({'is_active': False}) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        cancelled_reminders.append(reminder)
                        logger.info(f"Successfully canceled reminder {reminder['id']} (all cancellation)")
                    except Exception as e:
                        logger.error(f"Error cancelling reminder {reminder['id']}: {str(e)}")
                
                # Format response for cancelled reminders
                if cancelled_reminders:
                    response = f"🗑️ {len(cancelled_reminders)} lembretes cancelados com sucesso.\n\n"
                    response += "Você não tem mais lembretes ativos."
                    logger.info(f"All reminders cancelled successfully: {len(cancelled_reminders)}")
                    return response
                else:
                    logger.warning("Failed to cancel any reminders in 'all' mode")
                    return "❌ Não consegui cancelar os lembretes. Por favor, tente novamente."
            
            # Parse the cancellation request
            logger.info(f"Parsing cancellation request: '{normalized_text}'")
            cancel_data = parse_reminder(normalized_text, "cancelar")
            logger.info(f"Cancel data after parsing: {cancel_data}")
            
            # Initialize cancelled_reminders list
            cancelled_reminders = []
            
            # Check if we have valid cancel data
            if not cancel_data:
                logger.warning("Failed to parse cancellation request, showing reminder list")
                return format_reminder_list(reminders)
            
            # Log the cancel_type for debugging
            cancel_type = cancel_data.get("cancel_type")
            logger.info(f"Cancellation type: {cancel_type}")
            
            # Check for "all" cancellation
            if cancel_type == "all":
                logger.info(f"Cancelling ALL reminders for user {user_phone}")
                
                for reminder in reminders:
                    try:
                        logger.info(f"Attempting to cancel reminder {reminder['id']} (title: {reminder['title']})")
                        update_result = supabase.table('reminders') \
                            .update({'is_active': False}) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        cancelled_reminders.append(reminder)
                        logger.info(f"Successfully canceled reminder {reminder['id']} (all cancellation)")
                    except Exception as e:
                        logger.error(f"Error cancelling reminder {reminder['id']}: {str(e)}")
                
                # Format response for cancelled reminders
                if cancelled_reminders:
                    response = f"🗑️ {len(cancelled_reminders)} lembretes cancelados com sucesso.\n\n"
                    response += "Você não tem mais lembretes ativos."
                    logger.info(f"All reminders cancelled successfully: {len(cancelled_reminders)}")
                    return response
                else:
                    logger.warning("Failed to cancel any reminders in 'all' mode")
            
            # Check for range cancellation
            elif cancel_type == "range":
                start = cancel_data.get("range_start", 1)
                end = min(cancel_data.get("range_end", 1), len(reminders))
                logger.info(f"Cancelling RANGE from {start} to {end} for user {user_phone}")
                
                for i in range(start-1, end):  # Adjust for 0-based indexing
                    if i < len(reminders):
                        reminder = reminders[i]
                        logger.info(f"Attempting to cancel reminder #{i+1}: {reminder['title']}")
                        
                        try:
                            update_result = supabase.table('reminders') \
                                .update({'is_active': False}) \
                                .eq('id', reminder['id']) \
                                .execute()
                            
                            cancelled_reminders.append(reminder)
                            logger.info(f"Successfully canceled reminder {reminder['id']} (range cancellation)")
                        except Exception as e:
                            logger.error(f"Error cancelling reminder {reminder['id']}: {str(e)}")
            
            # Check for number cancellation
            elif cancel_type == "number":
                numbers = cancel_data.get("numbers", [])
                logger.info(f"Cancelling by NUMBERS: {numbers} for user {user_phone}")
                
                for num in numbers:
                    if 1 <= num <= len(reminders):
                        reminder = reminders[num-1]  # Adjust for 0-based indexing
                        logger.info(f"Attempting to cancel reminder #{num}: {reminder['title']}")
                        
                        try:
                            update_result = supabase.table('reminders') \
                                .update({'is_active': False}) \
                                .eq('id', reminder['id']) \
                                .execute()
                            
                            cancelled_reminders.append(reminder)
                            logger.info(f"Successfully canceled reminder {reminder['id']} (number {num})")
                        except Exception as e:
                            logger.error(f"Error cancelling reminder {reminder['id']}: {str(e)}")
            
            # Check for title cancellation
            elif cancel_type == "title":
                title_keywords = cancel_data.get("title", "").lower()
                logger.info(f"Cancelling by TITLE keywords: '{title_keywords}' for user {user_phone}")
                
                matching_reminders = []
                for reminder in reminders:
                    if title_keywords in reminder['title'].lower():
                        matching_reminders.append(reminder)
                
                logger.info(f"Found {len(matching_reminders)} matching reminders for title '{title_keywords}'")
                
                if not matching_reminders:
                    return f"❌ Não encontrei nenhum lembrete com '{title_keywords}' na descrição."
                
                if len(matching_reminders) > 1:
                    # If multiple matches, ask for clarification
                    response = "Encontrei vários lembretes que correspondem a essa descrição. Por favor, seja mais específico ou use o número do lembrete:\n\n"
                    
                    for i, reminder in enumerate(matching_reminders, 1):
                        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                        formatted_time = format_datetime(scheduled_time)
                        response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
                    
                    return response
                
                # If we have exactly one match
                reminder = matching_reminders[0]
                logger.info(f"Cancelling single matching reminder: {reminder['title']}")
                
                # Cancel the reminder
                try:
                    update_result = supabase.table('reminders') \
                        .update({'is_active': False}) \
                        .eq('id', reminder['id']) \
                        .execute()
                    
                    cancelled_reminders.append(reminder)
                    logger.info(f"Successfully canceled reminder {reminder['id']} by title")
                except Exception as e:
                    logger.error(f"Error cancelling reminder {reminder['id']}: {str(e)}")
            
            else:
                logger.warning(f"Unknown cancel_type: {cancel_type}")
            
            # Format response for cancelled reminders
            logger.info(f"Cancelled {len(cancelled_reminders)} reminders in total")
            
            if cancelled_reminders:
                # Get the updated list of active reminders
                remaining_reminders = list_reminders(user_phone)
                logger.info(f"User has {len(remaining_reminders)} remaining active reminders")
                
                if len(cancelled_reminders) == 1:
                    reminder = cancelled_reminders[0]
                    scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                    formatted_time = format_datetime(scheduled_time)
                    response = f"🗑️ Lembrete cancelado com sucesso:\n*{reminder['title']}* - {formatted_time}\n\n"
                else:
                    response = f"🗑️ {len(cancelled_reminders)} lembretes cancelados com sucesso:\n\n"
                    for i, reminder in enumerate(cancelled_reminders, 1):
                        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                        formatted_time = format_datetime(scheduled_time)
                        response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
                    response += "\n"
                
                # Add the list of remaining reminders
                if remaining_reminders:
                    response += "📋 *Seus lembretes restantes:*\n"
                    for i, reminder in enumerate(remaining_reminders, 1):
                        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                        formatted_time = format_datetime(scheduled_time)
                        response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
                else:
                    response += "Você não tem mais lembretes ativos."
                
                return response
            else:
                logger.warning("No reminders were cancelled")
                return "❌ Não consegui cancelar nenhum lembrete. Por favor, verifique o número ou a descrição."
        
        # Verificar se é uma solicitação para criar lembretes
        create_keywords = ["lembrar", "lembre", "lembra", "criar lembrete", "novo lembrete"]
        is_create_request = any(keyword in normalized_text for keyword in create_keywords)
        
        if is_create_request:
            logger.info("Detected create reminder request")
            # Extrair detalhes dos lembretes
            reminder_data = parse_reminder(normalized_text, "criar")
            logger.info(f"Reminder data after parsing: {reminder_data}")
            
            if reminder_data and "reminders" in reminder_data and reminder_data["reminders"]:
                logger.info(f"Found {len(reminder_data['reminders'])} reminders in parsed data")
                # Processar múltiplos lembretes
                created_reminders = []
                
                for reminder in reminder_data["reminders"]:
                    logger.info(f"Processing reminder: {reminder}")
                    if "title" in reminder and "scheduled_time" in reminder:
                        # Criar o lembrete
                        reminder_id = create_reminder(user_phone, reminder["title"], reminder["scheduled_time"])
                        logger.info(f"Created reminder with ID: {reminder_id}")
                        
                        if reminder_id:
                            created_reminders.append({
                                "title": reminder["title"],
                                "time": reminder["scheduled_time"]
                            })
                        else:
                            logger.error(f"Failed to create reminder: {reminder}")
                
                # Formatar resposta para múltiplos lembretes
                if created_reminders:
                    if len(created_reminders) == 1:
                        reminder = created_reminders[0]
                        return f"✅ Lembrete criado: {reminder['title']} para {format_datetime(reminder['time'])}"
                    else:
                        response = f"✅ {len(created_reminders)} lembretes criados:\n\n"
                        for i, reminder in enumerate(created_reminders, 1):
                            response += f"{i}. *{reminder['title']}* - {format_datetime(reminder['time'])}\n"
                        return response
                else:
                    logger.error("Failed to create any reminders despite valid parsing")
                    return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
            else:
                logger.error(f"Invalid reminder data structure: {reminder_data}")
                return "❌ Não consegui entender os detalhes do lembrete. Por favor, tente novamente com mais informações."
        
        # Se chegou aqui, não foi possível identificar a intenção específica
        logger.info(f"Reminder intent detected: {normalized_text.split()[0] if normalized_text else 'empty'}")
        return None
        
    except Exception as e:
        logger.error(f"Error handling reminder intent: {str(e)}")
        return "Desculpe, ocorreu um erro ao processar seu pedido de lembrete."

def process_reminder(user_phone, title, time_str):
    """Processa a criação de um novo lembrete"""
    try:
        # Converter data/hora para timestamp
        scheduled_time = parse_datetime_with_llm(time_str)
        
        # Criar o lembrete
        reminder_id = create_reminder(user_phone, title, scheduled_time)
        
        if reminder_id:
            return f"✅ Lembrete criado: {title} para {format_datetime(scheduled_time)}"
        else:
            return "❌ Não consegui criar o lembrete. Por favor, tente novamente."
            
    except Exception as e:
        logger.error(f"Error processing reminder: {str(e)}")
        return "❌ Não consegui processar o lembrete. Por favor, tente novamente." 

def send_reminder_notification(reminder):
    """Envia uma notificação de lembrete via Twilio"""
    try:
        user_phone = reminder['user_phone']
        
        # Verificar se é um lembrete atrasado
        if reminder.get('is_late'):
            # Verificar se temos as funções de fuso horário
            if 'to_local_timezone' in globals() and 'format_datetime' in globals():
                scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                formatted_time = format_datetime(scheduled_time, to_local=True)
                message = f"🔔 *LEMBRETE ATRASADO*: {reminder['title']}\n\n(Este lembrete estava programado para {formatted_time})"
            else:
                message = f"🔔 *LEMBRETE ATRASADO*: {reminder['title']}"
        else:
            message = f"🔔 *LEMBRETE*: {reminder['title']}"
        
        logger.info(f"Sending reminder to {user_phone}: {message}")
        
        # Garantir que o número tenha o prefixo whatsapp:
        if not user_phone.startswith('whatsapp:'):
            to_number = f"whatsapp:{user_phone}"
        else:
            to_number = user_phone
        
        # Enviar diretamente via Twilio (método mais confiável)
        try:
            direct_message = twilio_client.messages.create(
                body=message,
                from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
                to=to_number
            )
            
            logger.info(f"Reminder sent directly via Twilio: {direct_message.sid}")
            success = True
        except TwilioRestException as twilio_error:
            error_code = getattr(twilio_error, 'code', None)
            error_status = getattr(twilio_error, 'status', None)
            
            # Verificar se é um erro de limite de mensagens (429)
            if error_status == 429 or (hasattr(twilio_error, 'msg') and '429' in str(twilio_error.msg)):
                logger.error(f"RATE LIMIT EXCEEDED: Twilio daily message limit reached. Error: {str(twilio_error)}")
                success = False
            else:
                logger.error(f"Failed to send reminder via Twilio: {str(twilio_error)}")
                success = False
        except Exception as general_error:
            logger.error(f"General error sending reminder via Twilio: {str(general_error)}")
            success = False
        
        # Armazenar a notificação na tabela de conversas
        try:
            store_conversation(
                user_phone=user_phone.replace('whatsapp:', ''),
                message_content=message,
                message_type='text',
                is_from_user=False,
                agent="REMINDER"
            )
        except Exception as e:
            logger.error(f"Error storing reminder conversation: {str(e)}")
        
        return success
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
                time_module.sleep(check_interval)
                
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
            logger.info(f"Checking for reminder intent in transcribed audio: '{transcribed_text[:50]}...' (truncated)")
            is_reminder, action = detect_reminder_intent_with_llm(transcribed_text)
            
            if is_reminder:
                logger.info(f"Reminder intent detected in audio: {action}")
                
                if action == "clarify":
                    logger.info("Sending clarification message for ambiguous reminder intent in audio")
                    # User mentioned "lembrete" but intent is unclear
                    full_response = "O que você gostaria de fazer com seus lembretes? Você pode:\n\n" + \
                                    "• Ver seus lembretes (envie 'meus lembretes')\n" + \
                                    "• Criar um lembrete (ex: 'me lembra de pagar a conta amanhã')\n" + \
                                    "• Cancelar um lembrete (ex: 'cancelar lembrete 2')"
                    
                    # Store the agent's response
                    store_conversation(
                        user_phone=user_phone,
                        message_content=full_response,
                        message_type='text',
                        is_from_user=False
                    )
                    
                    # Return the response
                    resp = MessagingResponse()
                    resp.message(full_response)
                    return str(resp)
                
                # Handle the reminder intent
                logger.info(f"Handling reminder intent: {action}")
                start_time = time_module.time()
                reminder_response = handle_reminder_intent(user_phone, transcribed_text)
                elapsed_time = time_module.time() - start_time
                
                # Log the full response for debugging
                logger.info(f"Reminder handling completed in {elapsed_time:.2f}s")
                logger.info(f"Full reminder response: {reminder_response}")
                
                if reminder_response:
                    # Store the agent's response
                    store_conversation(
                        user_phone=user_phone,
                        message_content=reminder_response,
                        message_type='text',
                        is_from_user=False
                    )
                    
                    # Return the response
                    resp = MessagingResponse()
                    resp.message(reminder_response)
                    return str(resp)
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
            logger.info(f"Checking for reminder intent in message: '{incoming_msg[:50]}...' (truncated)")
            is_reminder, action = detect_reminder_intent_with_llm(incoming_msg)
            
            if is_reminder:
                logger.info(f"Reminder intent detected: {action}")
                
                if action == "clarify":
                    logger.info("Sending clarification message for ambiguous reminder intent")
                    # User mentioned "lembrete" but intent is unclear
                    full_response = "O que você gostaria de fazer com seus lembretes? Você pode:\n\n" + \
                                    "• Ver seus lembretes (envie 'meus lembretes')\n" + \
                                    "• Criar um lembrete (ex: 'me lembra de pagar a conta amanhã')\n" + \
                                    "• Cancelar um lembrete (ex: 'cancelar lembrete 2')"
                    
                    # Store the agent's response
                    store_conversation(
                        user_phone=user_phone,
                        message_content=full_response,
                        message_type='text',
                        is_from_user=False
                    )
                    
                    # Return the response
                    resp = MessagingResponse()
                    resp.message(full_response)
                    return str(resp)
                
                # Handle the reminder intent
                logger.info(f"Handling reminder intent: {action}")
                start_time = time_module.time()
                reminder_response = handle_reminder_intent(user_phone, incoming_msg)
                elapsed_time = time_module.time() - start_time
                
                # Log the full response for debugging
                logger.info(f"Reminder handling completed in {elapsed_time:.2f}s")
                logger.info(f"Full reminder response: {reminder_response}")
                
                if reminder_response:
                    # Store the agent's response
                    store_conversation(
                        user_phone=user_phone,
                        message_content=reminder_response,
                        message_type='text',
                        is_from_user=False
                    )
                    
                    # Return the response
                    resp = MessagingResponse()
                    resp.message(reminder_response)
                    return str(resp)
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

def check_and_send_reminders():
    """Verifica e envia lembretes pendentes"""
    try:
        logger.info("Checking for pending reminders...")
        
        # Obter a data e hora atual em UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds for comparison
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Buscar lembretes ativos que estão programados para antes ou no horário atual
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .execute()
        
        reminders = result.data
        logger.info(f"Found {len(reminders)} active reminders")
        
        # Filter reminders manually to ignore seconds
        pending_reminders = []
        for reminder in reminders:
            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
            # Truncate seconds for comparison
            scheduled_time_truncated = scheduled_time.replace(second=0, microsecond=0)
            if scheduled_time_truncated <= now_truncated:
                pending_reminders.append(reminder)
        
        logger.info(f"Found {len(pending_reminders)} pending reminders after time comparison")
        
        sent_count = 0
        failed_count = 0
        
        # Processar cada lembrete
        for reminder in pending_reminders:
            # Enviar a notificação
            success = send_reminder_notification(reminder)
            
            if success:
                # Mark as inactive
                update_result = supabase.table('reminders') \
                    .update({'is_active': False}) \
                    .eq('id', reminder['id']) \
                    .execute()
                
                logger.info(f"Reminder {reminder['id']} marked as inactive after sending")
                sent_count += 1
            else:
                logger.warning(f"Failed to send reminder {reminder['id']}, will try again later")
                failed_count += 1
        
        # Also update check_late_reminders function with similar logic
        late_results = check_late_reminders()
        
        return {
            "success": True, 
            "processed": len(pending_reminders),
            "sent": sent_count,
            "failed": failed_count,
            "late_processed": late_results.get("processed", 0),
            "late_sent": late_results.get("sent", 0),
            "late_failed": late_results.get("failed", 0),
            "late_deactivated": late_results.get("deactivated", 0)
        }
    
    except Exception as e:
        logger.error(f"Error checking reminders: {str(e)}")
        return {"success": False, "error": str(e)}

def check_late_reminders():
    """Verifica lembretes atrasados que não foram enviados após várias tentativas"""
    try:
        # Obter a data e hora atual em UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Definir um limite de tempo para considerar um lembrete como atrasado (ex: 30 minutos)
        late_threshold = now_truncated - timedelta(minutes=30)
        
        # Buscar lembretes ativos
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .execute()
        
        reminders = result.data
        
        # Filter late reminders manually
        late_reminders = []
        for reminder in reminders:
            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
            # Truncate seconds
            scheduled_time_truncated = scheduled_time.replace(second=0, microsecond=0)
            if scheduled_time_truncated <= late_threshold:
                late_reminders.append(reminder)
        
        processed = 0
        sent = 0
        failed = 0
        deactivated = 0
        
        if late_reminders:
            logger.info(f"Found {len(late_reminders)} late reminders")
            processed = len(late_reminders)
            
            for reminder in late_reminders:
                # Tentar enviar o lembrete atrasado
                success = send_reminder_notification(reminder)
                
                if success:
                    # Mark as inactive
                    update_result = supabase.table('reminders') \
                        .update({'is_active': False}) \
                        .eq('id', reminder['id']) \
                        .execute()
                    
                    logger.info(f"Late reminder {reminder['id']} marked as inactive after sending")
                    sent += 1
                else:
                    # For very late reminders (over 2 hours), deactivate them
                    very_late_threshold = now_truncated - timedelta(hours=2)
                    scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                    
                    if scheduled_time <= very_late_threshold:
                        update_result = supabase.table('reminders') \
                            .update({'is_active': False}) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        logger.warning(f"Deactivated very late reminder {reminder['id']} (over 2 hours late)")
                        deactivated += 1
                    else:
                        logger.warning(f"Failed to send late reminder {reminder['id']}, will try again later")
                        failed += 1
        
        return {
            "processed": processed,
            "sent": sent,
            "failed": failed,
            "deactivated": deactivated
        }
    
    except Exception as e:
        logger.error(f"Error checking late reminders: {str(e)}")
        return {
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "deactivated": 0,
            "error": str(e)
        }

# Atualizar o endpoint para usar a função
@app.route('/api/check-reminders', methods=['POST'])
def api_check_reminders():
    """Endpoint para verificar e enviar lembretes pendentes"""
    try:
        # Verificar autenticação
        api_key = request.headers.get('X-API-Key')
        if api_key != os.getenv('REMINDER_API_KEY'):
            return jsonify({"error": "Unauthorized"}), 401
        
        # Processar lembretes
        result = check_and_send_reminders()
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error in check-reminders endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

def detect_reminder_intent_with_llm(message):
    """Detecta a intenção relacionada a lembretes usando o LLM"""
    try:
        logger.info(f"Detecting reminder intent with LLM for message: '{message[:50]}...' (truncated)")
        
        system_prompt = """
        Você é um assistente especializado em detectar intenções relacionadas a lembretes em mensagens em português.
        
        Analise a mensagem do usuário e identifique se ela contém uma intenção relacionada a lembretes.
        
        Retorne um JSON com o seguinte formato:
        {
          "is_reminder": true/false,
          "intent": "criar" ou "listar" ou "cancelar" ou "clarify" ou null
        }
        
        Onde:
        - "is_reminder": indica se a mensagem contém uma intenção relacionada a lembretes
        - "intent": o tipo específico de intenção
          - "criar": para criar um novo lembrete
          - "listar": para listar lembretes existentes
          - "cancelar": para cancelar um lembrete existente
          - "clarify": quando menciona lembretes mas a intenção não está clara
          - null: quando não é uma intenção relacionada a lembretes
        
        Exemplos:
        - "me lembra de pagar a conta amanhã" → {"is_reminder": true, "intent": "criar"}
        - "meus lembretes" → {"is_reminder": true, "intent": "listar"}
        - "cancelar lembrete 2" → {"is_reminder": true, "intent": "cancelar"}
        - "lembrete" → {"is_reminder": true, "intent": "clarify"}
        - "como está o tempo hoje?" → {"is_reminder": false, "intent": null}
        """
        
        start_time = time_module.time()
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        elapsed_time = time_module.time() - start_time
        
        result = json.loads(response.choices[0].message.content)
        logger.info(f"LLM intent detection result: {result} (took {elapsed_time:.2f}s)")
        
        is_reminder = result.get("is_reminder", False)
        intent = result.get("intent")
        
        logger.info(f"Intent detection result: is_reminder={is_reminder}, intent={intent}")
        
        return is_reminder, intent
        
    except Exception as e:
        logger.error(f"Error in LLM intent detection: {str(e)}")
        logger.info("Falling back to keyword-based detection")
        # Fall back to keyword-based detection if LLM fails
        return detect_reminder_intent(message)