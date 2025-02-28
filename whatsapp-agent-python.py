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

def format_datetime(dt):
    """Formata um datetime para exibição amigável em português"""
    # Convert to local timezone
    local_dt = to_local_timezone(dt)
    
    # Get current date in local timezone
    now = datetime.now(BRAZIL_TIMEZONE)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    # Format the date part
    if local_dt.date() == today:
        date_str = "hoje"
    elif local_dt.date() == tomorrow:
        date_str = "amanhã"
    else:
        # Format the date in Portuguese
        date_str = local_dt.strftime("%d/%m/%Y")
    
    # Format the time
    time_str = local_dt.strftime("%H:%M")
    
    return f"{date_str} às {time_str}"

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
        current_year = now_local.year
        current_month = now_local.month
        current_day = now_local.day
        tomorrow = now_local + timedelta(days=1)
        tomorrow_day = tomorrow.day
        tomorrow_month = tomorrow.month
        tomorrow_year = tomorrow.year
        
        system_prompt = ""
        
        if action == "criar":
            system_prompt = f"""
            Você é um assistente especializado em extrair informações de lembretes em português.
            
            A data atual é: {now_local.strftime('%d/%m/%Y')} (dia/mês/ano)
            A hora atual é: {now_local.strftime('%H:%M')} (formato 24h)
            Amanhã será: {tomorrow.strftime('%d/%m/%Y')} (dia/mês/ano)
            
            Analise a mensagem do usuário e extraia os detalhes do lembrete, incluindo título e data/hora.
            
            Retorne um JSON com o seguinte formato:
            {{
              "reminders": [
                {{
                  "title": "título do lembrete",
                  "datetime": {{
                    "year": ano (número),
                    "month": mês (número),
                    "day": dia (número),
                    "hour": hora (número em formato 24h),
                    "minute": minuto (número)
                  }}
                }}
              ]
            }}
            
            Regras:
            1. Se o usuário não especificar o ano, use o ano atual ({current_year}).
            2. Se o usuário não especificar o mês, use o mês atual ({current_month}).
            3. Se o usuário não especificar a hora, use 12:00 (meio-dia).
            4. Se o usuário mencionar "amanhã", use dia={tomorrow_day}, mês={tomorrow_month}, ano={tomorrow_year}.
            5. Se o usuário mencionar "próxima semana", adicione 7 dias à data atual.
            6. Se o usuário mencionar um dia da semana (ex: "segunda"), use a próxima ocorrência desse dia.
            7. Interprete expressões como "daqui a 2 dias" ou "em 3 horas" corretamente.
            8. Se o usuário mencionar múltiplos lembretes, inclua cada um como um item separado no array "reminders".
            9. IMPORTANTE: Tente evitar criar lembretes no passado. Se o usuário pedir um lembrete para um horário que já passou hoje, assuma que é para amanhã.
            """
        elif action == "cancelar":
            system_prompt = """
            Você é um assistente especializado em extrair informações de cancelamento de lembretes em português.
            
            Analise a mensagem do usuário e identifique qual lembrete o usuário deseja cancelar.
            
            Retorne um JSON com o seguinte formato:
            {
              "cancel_type": "number" ou "range" ou "title" ou "all",
              "numbers": [lista de números] (se cancel_type for "number"),
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
        
        # Post-process dates to ensure they're in the future
        if action == "criar" and "reminders" in parsed_response:
            for reminder in parsed_response["reminders"]:
                if "datetime" in reminder:
                    dt_components = reminder["datetime"]
                    
                    # Ensure year is current or future
                    if dt_components.get("year", current_year) < current_year:
                        logger.info(f"Correcting past year {dt_components.get('year')} to current year {current_year}")
                        dt_components["year"] = current_year
                    
                    # Create a datetime object to check if it's in the past
                    try:
                        # Create a naive datetime first
                        naive_dt = datetime(
                            year=dt_components.get("year", current_year),
                            month=dt_components.get("month", current_month),
                            day=dt_components.get("day", current_day),
                            hour=dt_components.get("hour", 12),
                            minute=dt_components.get("minute", 0)
                        )
                        
                        # Add timezone info to make it aware
                        dt = brazil_tz.localize(naive_dt)
                        
                        # Special handling for "tomorrow" - check if the date should be tomorrow
                        if "amanhã" in message.lower() or "amanha" in message.lower():
                            tomorrow = now_local + timedelta(days=1)
                            # Only update if the date is not already set to tomorrow or later
                            if dt.date() < tomorrow.date():
                                logger.info(f"Adjusting date to tomorrow because 'amanhã' was mentioned")
                                dt = dt.replace(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day)
                                # Update the components
                                dt_components["year"] = tomorrow.year
                                dt_components["month"] = tomorrow.month
                                dt_components["day"] = tomorrow.day
                        
                        # If the datetime is in the past, adjust it
                        if dt < now_local:
                            logger.info(f"Detected past date: {dt}, adjusting to future")
                            # If it's today but earlier time, move to tomorrow
                            if dt.date() == now_local.date():
                                dt = dt + timedelta(days=1)
                                # Update the components
                                dt_components["year"] = dt.year
                                dt_components["month"] = dt.month
                            # If it's an earlier date this year, but not more than 30 days in the past,
                            # assume it's next month
                            elif dt.year == current_year and (now_local.date() - dt.date()).days < 30:
                                # Add one month
                                if dt.month == 12:
                                    dt = dt.replace(year=dt.year + 1, month=1)
                                else:
                                    dt = dt.replace(month=dt.month + 1)
                                # Update the components
                                dt_components["year"] = dt.year
                                dt_components["month"] = dt.month
                            # Otherwise, assume it's next year
                            else:
                                dt = dt.replace(year=dt.year + 1)
                                # Update the components
                                dt_components["year"] = dt.year
                    except Exception as e:
                        logger.error(f"Error post-processing date: {str(e)}")
        
        return parsed_response
        
    except Exception as e:
        logger.error(f"Error parsing reminder with LLM: {str(e)}")
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

def handle_reminder_intent(user_phone, message_text):
    """Processa intenções relacionadas a lembretes"""
    try:
        # Normalizar o texto da mensagem
        if isinstance(message_text, (list, tuple)):
            message_text = ' '.join(str(x) for x in message_text)  # Convert list to string safely
        normalized_text = str(message_text).lower().strip()
        
        logger.info(f"Processing reminder intent with normalized text: '{normalized_text}'")
        
        # Special case for "cancelar todos os lembretes" - handle it directly
        if "cancelar todos" in normalized_text or "excluir todos" in normalized_text or "apagar todos" in normalized_text:
            logger.info("Detected cancel all reminders request")
            
            # Get all active reminders for this user
            reminders = list_reminders(user_phone)
            
            if not reminders:
                return "Você não tem lembretes ativos para cancelar."
            
            # Cancel all reminders
            canceled_count = 0
            for reminder in reminders:
                success = cancel_reminder(reminder['id'])
                if success:
                    canceled_count += 1
            
            if canceled_count > 0:
                return f"✅ {canceled_count} lembretes foram cancelados com sucesso."
            else:
                return "❌ Não foi possível cancelar os lembretes. Por favor, tente novamente."
        
        # Use the same list_keywords as in detect_reminder_intent
        list_keywords = ["lembretes", "meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes"]
        if any(keyword in normalized_text for keyword in list_keywords):
            logger.info("Detected list reminders request")
            reminders = list_reminders(user_phone)
            logger.info(f"Found {len(reminders)} reminders to list")
            return format_reminder_list_by_time(reminders)
            
        # Verificar se é uma solicitação para cancelar lembretes
        cancel_keywords = ["cancelar", "remover", "apagar", "deletar", "excluir"]
        is_cancel_request = any(keyword in normalized_text for keyword in cancel_keywords)
        
        # Verificar se é uma solicitação para criar lembretes
        create_keywords = ["lembrar", "lembra", "lembre", "criar lembrete", "novo lembrete", "adicionar lembrete"]
        is_create_request = any(keyword in normalized_text for keyword in create_keywords)
        
        if is_cancel_request:
            logger.info("Detected cancel reminder request")
            
            # First, get the list of active reminders for this user
            reminders = list_reminders(user_phone)
            logger.info(f"Found {len(reminders)} active reminders for user {user_phone}")
            
            if not reminders:
                return "Você não tem lembretes ativos para cancelar."
            
            # Parse the cancellation request
            cancel_data = parse_reminder(normalized_text, "cancelar")
            logger.info(f"Cancel data after parsing: {cancel_data}")
            
            if cancel_data and "cancel_type" in cancel_data:
                cancel_type = cancel_data["cancel_type"]
                
                # Handle different cancellation types
                if cancel_type == "all":
                    logger.info("Cancelling all reminders")
                    canceled_count = 0
                    for reminder in reminders:
                        success = cancel_reminder(reminder['id'])
                        if success:
                            canceled_count += 1
                    
                    if canceled_count > 0:
                        return f"✅ {canceled_count} lembretes foram cancelados com sucesso."
                    else:
                        return "❌ Não foi possível cancelar os lembretes. Por favor, tente novamente."
                
                elif cancel_type == "number":
                    numbers = cancel_data.get("numbers", [])
                    logger.info(f"Cancelling reminders by numbers: {numbers}")
                    
                    if not numbers:
                        return "Por favor, especifique quais lembretes deseja cancelar pelo número."
                    
                    canceled = []
                    not_found = []
                    
                    for num in numbers:
                        if 1 <= num <= len(reminders):
                            reminder = reminders[num-1]  # Adjust for 0-based indexing
                            success = cancel_reminder(reminder['id'])
                            if success:
                                canceled.append(reminder)
                        else:
                            not_found.append(num)
                    
                    # Prepare response
                    if canceled:
                        response = f"✅ {len(canceled)} lembretes cancelados:\n"
                        for reminder in canceled:
                            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                            response += f"- *{reminder['title']}* para {format_datetime(scheduled_time)}\n"
                        
                        # Add info about remaining reminders
                        remaining = list_reminders(user_phone)
                        if remaining:
                            response += f"\nVocê ainda tem {len(remaining)} lembretes ativos."
                        
                        return response
                    else:
                        return f"❌ Não foi possível encontrar os lembretes com os números especificados."
                
                elif cancel_type == "range":
                    range_start = cancel_data.get("range_start", 1)
                    range_end = cancel_data.get("range_end", len(reminders))
                    logger.info(f"Cancelling reminders in range: {range_start} to {range_end}")
                    
                    # Validate range
                    if range_start < 1:
                        range_start = 1
                    if range_end > len(reminders):
                        range_end = len(reminders)
                    
                    canceled = []
                    for i in range(range_start-1, range_end):  # Adjust for 0-based indexing
                        if i < len(reminders):
                            reminder = reminders[i]
                            success = cancel_reminder(reminder['id'])
                            if success:
                                canceled.append(reminder)
                    
                    # Prepare response
                    if canceled:
                        response = f"✅ {len(canceled)} lembretes cancelados:\n"
                        for reminder in canceled:
                            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                            response += f"- *{reminder['title']}* para {format_datetime(scheduled_time)}\n"
                        
                        # Add info about remaining reminders
                        remaining = list_reminders(user_phone)
                        if remaining:
                            response += f"\nVocê ainda tem {len(remaining)} lembretes ativos."
                        
                        return response
                    else:
                        return "❌ Não foi possível cancelar os lembretes no intervalo especificado."
                
                elif cancel_type == "title":
                    title = cancel_data.get("title", "").lower()
                    logger.info(f"Cancelling reminders by title: {title}")
                    
                    if not title:
                        return "Por favor, especifique o título ou palavras-chave do lembrete que deseja cancelar."
                    
                    canceled = []
                    for reminder in reminders:
                        if title in reminder['title'].lower():
                            success = cancel_reminder(reminder['id'])
                            if success:
                                canceled.append(reminder)
                    
                    # Prepare response
                    if canceled:
                        response = f"✅ {len(canceled)} lembretes cancelados:\n"
                        for reminder in canceled:
                            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                            response += f"- *{reminder['title']}* para {format_datetime(scheduled_time)}\n"
                        
                        # Add info about remaining reminders
                        remaining = list_reminders(user_phone)
                        if remaining:
                            response += f"\nVocê ainda tem {len(remaining)} lembretes ativos."
                        
                        return response
                    else:
                        return f"❌ Não foi possível encontrar lembretes com o título '{title}'."
            
            # If we get here, we couldn't parse the cancellation request
            return "Não entendi qual lembrete você deseja cancelar. Por favor, especifique o número ou título do lembrete."
        
        elif is_create_request:
            logger.info("Detected create reminder request")
            # Parse the reminder data
            reminder_data = parse_reminder(normalized_text, "criar")
            logger.info(f"Reminder data after parsing: {reminder_data}")
            
            if reminder_data and "reminders" in reminder_data and reminder_data["reminders"]:
                logger.info(f"Found {len(reminder_data['reminders'])} reminders in parsed data")
                # Processar múltiplos lembretes
                created_reminders = []
                invalid_reminders = []
                
                for reminder in reminder_data["reminders"]:
                    logger.info(f"Processing reminder: {reminder}")
                    if "title" in reminder and "datetime" in reminder:
                        # Process datetime components
                        dt_components = reminder["datetime"]
                        try:
                            # Create datetime object from components
                            brazil_tz = pytz.timezone('America/Sao_Paulo')
                            now_local = datetime.now(brazil_tz)
                            
                            # Create a naive datetime first
                            naive_dt = datetime(
                                year=dt_components.get('year', now_local.year),
                                month=dt_components.get('month', now_local.month),
                                day=dt_components.get('day', now_local.day),
                                hour=dt_components.get('hour', 12),
                                minute=dt_components.get('minute', 0),
                                second=0,
                                microsecond=0
                            )
                            
                            # Add timezone info to make it aware
                            dt = brazil_tz.localize(naive_dt)
                            
                            # Check if the reminder is in the past
                            if dt < now_local:
                                logger.warning(f"Attempted to create reminder in the past: {reminder['title']} at {dt}")
                                invalid_reminders.append({
                                    "title": reminder["title"],
                                    "time": dt,
                                    "reason": "past"
                                })
                                continue
                            
                            # Convert to UTC
                            scheduled_time = dt.astimezone(timezone.utc)
                            
                            # Criar o lembrete
                            reminder_id = create_reminder(user_phone, reminder["title"], scheduled_time)
                            
                            if reminder_id:
                                created_reminders.append({
                                    "title": reminder["title"],
                                    "time": scheduled_time
                                })
                                logger.info(f"Created reminder {reminder_id}: {reminder['title']} at {scheduled_time}")
                            else:
                                logger.error(f"Failed to create reminder: {reminder['title']}")
                        except Exception as e:
                            logger.error(f"Error processing datetime: {str(e)}")
                
                # Formatar resposta para múltiplos lembretes
                if created_reminders:
                    response = format_created_reminders(created_reminders)
                    
                    # Add warning about invalid reminders if any
                    if invalid_reminders:
                        past_reminders = [r for r in invalid_reminders if r.get("reason") == "past"]
                        if past_reminders:
                            response += "\n\n⚠️ Não foi possível criar os seguintes lembretes porque estão no passado:\n"
                            for i, reminder in enumerate(past_reminders, 1):
                                response += f"- *{reminder['title']}* para {format_datetime(reminder['time'])}\n"
                    
                    return response
                elif invalid_reminders:
                    # All reminders were invalid
                    past_reminders = [r for r in invalid_reminders if r.get("reason") == "past"]
                    if past_reminders:
                        response = "❌ Não foi possível criar os lembretes porque estão no passado:\n\n"
                        for i, reminder in enumerate(past_reminders, 1):
                            response += f"{i}. *{reminder['title']}* para {format_datetime(reminder['time'])}\n"
                        response += "\nPor favor, especifique uma data e hora no futuro."
                        return response
                    else:
                        return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
                else:
                    logger.warning("Failed to create any reminders")
                    return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
            else:
                logger.warning("Failed to parse reminder creation request")
                return "❌ Não consegui entender os detalhes do lembrete. Por favor, especifique o título e quando deseja ser lembrado."
        
        # If we get here, we couldn't determine the intent
        logger.warning(f"Could not determine specific reminder intent for: '{normalized_text}'")
        return "Não entendi o que você deseja fazer com os lembretes. Você pode criar, listar ou cancelar lembretes."
        
    except Exception as e:
        logger.error(f"Error in handle_reminder_intent: {str(e)}")
        return "Ocorreu um erro ao processar sua solicitação de lembrete. Por favor, tente novamente."

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
    """Envia uma notificação de lembrete para o usuário"""
    try:
        user_phone = reminder['user_phone']
        title = reminder['title']
        
        # Format the message
        message_body = f"🔔 *LEMBRETE*: {title}"
        
        logger.info(f"Sending reminder to {user_phone}: {message_body}")
        
        # Try to queue the message first
        queue_success = send_whatsapp_message(user_phone, message_body)
        
        # If queueing fails, try direct send
        if not queue_success:
            logger.warning(f"Queue failed, trying direct send to {user_phone}")
            try:
                # Format the number for Twilio
                if not user_phone.startswith('whatsapp:'):
                    to_number = f"whatsapp:{user_phone}"
                else:
                    to_number = user_phone
                
                # Send directly
                message = twilio_client.messages.create(
                    body=message_body,
                    from_=f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}",
                    to=to_number
                )
                logger.info(f"Direct send successful: {message.sid}")
                queue_success = True
            except Exception as e:
                logger.error(f"Direct send failed: {str(e)}")
        
        # Store in conversation history
        store_conversation(user_phone, message_body, 'text', False, agent="REMINDER")
        
        return queue_success
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
    """Checks for pending reminders and sends notifications"""
    try:
        logger.info("Checking for pending reminders...")
        
        # Get current time in UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds for comparison
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Get all active reminders
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
            
            # Only include reminders that are due (current time >= scheduled time)
            # Add a debug log to see the comparison
            logger.info(f"Comparing reminder time {scheduled_time_truncated} with current time {now_truncated}")
            if now_truncated >= scheduled_time_truncated:
                logger.info(f"Reminder {reminder['id']} is due")
                pending_reminders.append(reminder)
            else:
                logger.info(f"Reminder {reminder['id']} is not yet due")
        
        logger.info(f"Found {len(pending_reminders)} pending reminders after time comparison")
        
        sent_count = 0
        failed_count = 0
        
        for reminder in pending_reminders:
            # Send notification
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
        
        # Also check late reminders
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
        return {"error": str(e)}

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
        
        # Ensure message sender thread is running
        global message_sender_thread
        if not hasattr(app, 'message_sender_thread') or not app.message_sender_thread.is_alive():
            logger.info("Starting message sender thread for reminder check")
            app.message_sender_thread = start_message_sender()
        
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

if __name__ == '__main__':
    # Start the message sender thread
    app.message_sender_thread = start_message_sender()
    
    # Start the self-ping thread if needed
    if os.getenv('ENABLE_SELF_PING', 'false').lower() == 'true':
        start_self_ping()
    
    # Start the Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)