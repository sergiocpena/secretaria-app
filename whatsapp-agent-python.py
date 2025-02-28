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
    list_keywords = ["lembretes", "meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes"]
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
            Você é um assistente especializado em extrair informações de lembretes de mensagens em português.
            
            Extraia as informações de lembrete da mensagem do usuário.
            Se houver múltiplos lembretes, retorne um array de objetos.
            
            Retorne um JSON com o seguinte formato:
            {
              "reminders": [
                {
                  "title": "título ou assunto do lembrete",
                  "date": "data do lembrete (formato YYYY-MM-DD, ou 'hoje', 'amanhã')",
                  "time": "hora do lembrete (formato HH:MM)"
                }
              ]
            }
            
            Para tempos relativos como "daqui 5 minutos", "em 2 horas", "daqui 2h", coloque a expressão completa no campo "date" e deixe "time" vazio.
            
            Exemplos de expressões relativas que você deve reconhecer:
            - "daqui 5 minutos" -> date: "daqui 5 minutos", time: null
            - "daqui 2h" -> date: "daqui 2 horas", time: null
            - "em 1 hora" -> date: "daqui 1 hora", time: null
            
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
            response_format={"type": "json_object"},
            temperature=0.1  # Lower temperature for more consistent parsing
        )
        
        parsed_response = json.loads(response.choices[0].message.content)
        logger.info(f"Parsed reminder data: {parsed_response}")
        
        # Add fallback handling for old format responses
        if action == "criar" and "reminders" not in parsed_response and "title" in parsed_response:
            # Convert old format to new format
            return {
                "reminders": [
                    {
                        "title": parsed_response.get("title"),
                        "date": parsed_response.get("date"),
                        "time": parsed_response.get("time")
                    }
                ]
            }
            
        return parsed_response
    except Exception as e:
        logger.error(f"Error parsing reminder: {str(e)}")
        return None

def parse_datetime(date_str, time_str):
    """Converte strings de data e hora para um objeto datetime"""
    try:
        # Get current time in Brazil timezone first
        brazil_tz = pytz.timezone('America/Sao_Paulo')  # UTC-3
        now_local = datetime.now(brazil_tz)
        
        # Verificar se temos valores nulos
        if date_str is None:
            # Se não tiver data, assumir hoje
            date = now_local.date()
        elif date_str.lower() == 'hoje':
            date = now_local.date()
        elif date_str.lower() == 'amanhã' or date_str.lower() == 'amanha':
            date = (now_local + timedelta(days=1)).date()
        else:
            # Tentar interpretar expressões relativas como "daqui X minutos/horas/dias"
            relative_match = re.search(r'daqui\s+(\d+)\s*(minutos?|horas?|dias?|min|h)', date_str.lower())
            if relative_match:
                amount = int(relative_match.group(1))
                unit = relative_match.group(2)
                
                if 'minuto' in unit or unit == 'min':
                    return now_local + timedelta(minutes=amount)
                elif 'hora' in unit or unit == 'h':
                    return now_local + timedelta(hours=amount)
                elif 'dia' in unit:
                    return now_local + timedelta(days=amount)
            
            # Try to match "em X horas/minutos"
            em_match = re.search(r'em\s+(\d+)\s*(minutos?|horas?|dias?|min|h)', date_str.lower())
            if em_match:
                amount = int(em_match.group(1))
                unit = em_match.group(2)
                
                if 'minuto' in unit or unit == 'min':
                    return now_local + timedelta(minutes=amount)
                elif 'hora' in unit or unit == 'h':
                    return now_local + timedelta(hours=amount)
                elif 'dia' in unit:
                    return now_local + timedelta(days=amount)
            
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
                    date = (now_local + timedelta(days=1)).date()
        
        # Se já retornamos um datetime completo (caso de tempo relativo)
        if isinstance(date, datetime):
            # Ensure it's in the local timezone
            if date.tzinfo is None:
                date = brazil_tz.localize(date)
            # Convert to UTC for storage
            return date.astimezone(timezone.utc)
        
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
            
            # Criar o datetime combinado no fuso horário local
            dt = datetime.combine(date, datetime.min.time().replace(hour=hour, minute=minute))
            
            # Adicionar timezone local
            dt = brazil_tz.localize(dt)
            
            # Converter para UTC para armazenamento
            return dt.astimezone(timezone.utc)
        else:
            # Se não houver hora, usar meio-dia
            dt = datetime.combine(date, datetime.min.time().replace(hour=12, minute=0))
            dt = brazil_tz.localize(dt)
            return dt.astimezone(timezone.utc)
            
    except Exception as e:
        logger.error(f"Error parsing datetime: {str(e)}")
        # Retornar data/hora padrão (amanhã ao meio-dia)
        tomorrow_noon = (datetime.now(brazil_tz) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        return tomorrow_noon.astimezone(timezone.utc)

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

def handle_reminder_intent(user_phone, message_text):
    """Processa intenções relacionadas a lembretes"""
    try:
        # Normalizar o texto da mensagem
        if isinstance(message_text, (list, tuple)):
            message_text = ' '.join(str(x) for x in message_text)  # Convert list to string safely
        normalized_text = str(message_text).lower().strip()
        
        # Use the same list_keywords as in detect_reminder_intent
        list_keywords = ["lembretes", "meus lembretes", "listar lembretes", "ver lembretes", "mostrar lembretes"]
        if any(keyword in normalized_text for keyword in list_keywords):
            reminders = list_reminders(user_phone)
            return format_reminder_list(reminders)
            
        # Verificar se é uma solicitação para criar lembretes
        create_keywords = ["lembrar", "lembre", "lembra", "criar lembrete", "novo lembrete"]
        is_create_request = any(keyword in normalized_text for keyword in create_keywords)
        
        if is_create_request:
            # Extrair detalhes dos lembretes
            reminder_data = parse_reminder(normalized_text, "criar")
            
            if reminder_data and "reminders" in reminder_data and reminder_data["reminders"]:
                # Processar múltiplos lembretes
                created_reminders = []
                
                for reminder in reminder_data["reminders"]:
                    if "title" in reminder:
                        # Converter data/hora para timestamp
                        scheduled_time = parse_datetime(
                            reminder.get("date", "hoje"), 
                            reminder.get("time", None)
                        )
                        
                        # Criar o lembrete
                        reminder_id = create_reminder(user_phone, reminder["title"], scheduled_time)
                        
                        if reminder_id:
                            created_reminders.append({
                                "title": reminder["title"],
                                "time": scheduled_time
                            })
                
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
                    return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
            
        # Verificar se é uma solicitação para cancelar um lembrete
        cancel_keywords = ["cancelar", "remover", "apagar", "deletar"]
        is_cancel_request = any(keyword in normalized_text for keyword in cancel_keywords)
        
        if is_cancel_request:
            # Buscar todos os lembretes ativos do usuário primeiro
            result = supabase.table('reminders') \
                .select('*') \
                .eq('user_phone', user_phone) \
                .eq('is_active', True) \
                .order('scheduled_time', desc=False) \
                .execute()
            
            reminders = result.data
            
            if not reminders:
                return "Você não tem lembretes ativos para cancelar."
                
            # Se houver apenas um lembrete e o comando for genérico, cancelar esse único lembrete
            if len(reminders) == 1 and all(word in ["cancelar", "lembrete", "o", "meu"] for word in normalized_text.split()):
                reminder = reminders[0]
                
                # Cancelar o lembrete
                update_result = supabase.table('reminders') \
                    .update({'is_active': False}) \
                    .eq('id', reminder['id']) \
                    .execute()
                
                logger.info(f"Canceled only reminder {reminder['id']} for user {user_phone}")
                
                # Formatar a resposta
                scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                formatted_time = format_datetime(scheduled_time)
                
                return f"✅ Lembrete cancelado com sucesso:\n*{reminder['title']}* - {formatted_time}"
            
            # Verificar se é uma referência numérica (ex: "cancelar 2" ou "cancelar lembrete 2")
            number_match = re.search(r'(?:cancelar|remover|apagar|deletar)(?:\s+(?:o\s+)?(?:lembrete\s+)?)?(\d+)', normalized_text)
            
            if number_match:
                reminder_number = int(number_match.group(1))
                
                # Verificar se o número está dentro do intervalo válido
                if 1 <= reminder_number <= len(reminders):
                    # Obter o lembrete pelo índice
                    reminder = reminders[reminder_number - 1]
                    
                    # Cancelar o lembrete
                    update_result = supabase.table('reminders') \
                        .update({'is_active': False}) \
                        .eq('id', reminder['id']) \
                        .execute()
                    
                    logger.info(f"Canceled reminder {reminder['id']} for user {user_phone}")
                    
                    # Formatar a resposta
                    scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                    formatted_time = format_datetime(scheduled_time)
                    
                    return f"✅ Lembrete cancelado com sucesso:\n*{reminder['title']}* - {formatted_time}"
                else:
                    return f"Não encontrei o lembrete número {reminder_number}. Você tem {len(reminders)} lembretes ativos."
            
            # Se não for uma referência numérica, procurar por descrição
            # Remover palavras comuns de cancelamento para extrair a descrição
            description = normalized_text
            for word in ["cancelar", "remover", "apagar", "deletar", "lembrete", "o", "meu"]:
                description = description.replace(word, "").strip()
            
            if not description:
                # Se não houver descrição específica, mostrar a lista de lembretes
                return list_reminders(user_phone)
            
            # Procurar por lembretes que correspondam à descrição
            matching_reminders = []
            for reminder in reminders:
                if description.lower() in reminder['title'].lower():
                    matching_reminders.append(reminder)
            
            if not matching_reminders:
                return "❌ Não encontrei nenhum lembrete com essa descrição..."
            
            if len(matching_reminders) > 1:
                # Se houver múltiplos lembretes correspondentes, pedir para ser mais específico
                response = "Encontrei vários lembretes que correspondem a essa descrição. Por favor, seja mais específico ou use o número do lembrete:\n\n"
                
                for i, reminder in enumerate(matching_reminders, 1):
                    scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                    formatted_time = format_datetime(scheduled_time)
                    response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
                
                return response
            
            # Se chegou aqui, temos exatamente um lembrete correspondente
            reminder = matching_reminders[0]
            
            # Cancelar o lembrete
            update_result = supabase.table('reminders') \
                .update({'is_active': False}) \
                .eq('id', reminder['id']) \
                .execute()
            
            logger.info(f"Canceled reminder {reminder['id']} for user {user_phone}")
            
            # Formatar a resposta
            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
            formatted_time = format_datetime(scheduled_time)
            
            return f"✅ Lembrete cancelado com sucesso:\n*{reminder['title']}* - {formatted_time}"
        
        # Se chegou aqui, não foi possível identificar a intenção específica
        logger.info(f"Reminder intent detected: {normalized_text.split()[0] if normalized_text else 'empty'}")
        return None
        
    except Exception as e:
        logger.error(f"Error handling reminder intent: {str(e)}")
        return "Desculpe, ocorreu um erro ao processar sua solicitação de lembrete."

def process_reminder(user_phone, title, time_str):
    """Processa a criação de um novo lembrete"""
    try:
        # Converter data/hora para timestamp
        scheduled_time = parse_datetime(time_str, None)
        
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
                        cancelled = handle_reminder_intent(user_phone, reminder_data['keywords'])
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
                        cancelled = handle_reminder_intent(user_phone, reminder_data['keywords'])
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
                # Incrementar a contagem de tentativas
                retry_count = reminder.get('retry_count', 0) + 1
                
                update_result = supabase.table('reminders') \
                    .update({'retry_count': retry_count}) \
                    .eq('id', reminder['id']) \
                    .execute()
                
                logger.warning(f"Failed to send reminder {reminder['id']}, will try again later (attempt {retry_count})")
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
                # Marcar o lembrete como atrasado
                reminder['is_late'] = True
                
                # Tentar enviar o lembrete atrasado
                success = send_reminder_notification(reminder)
                
                if success:
                    # Mark as inactive
                    update_result = supabase.table('reminders') \
                        .update({'is_active': False, 'is_late': True}) \
                        .eq('id', reminder['id']) \
                        .execute()
                    
                    logger.info(f"Late reminder {reminder['id']} marked as inactive after sending")
                    sent += 1
                else:
                    # Incrementar a contagem de tentativas
                    retry_count = reminder.get('retry_count', 0) + 1
                    failed += 1
                    
                    # Se exceder o número máximo de tentativas, desativar o lembrete
                    if retry_count > 5:  # Máximo de 5 tentativas
                        update_result = supabase.table('reminders') \
                            .update({'is_active': False, 'is_late': True, 'retry_count': retry_count}) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        logger.warning(f"Deactivated reminder {reminder['id']} after {retry_count} failed attempts")
                        deactivated += 1
                    else:
                        update_result = supabase.table('reminders') \
                            .update({'is_late': True, 'retry_count': retry_count}) \
                            .eq('id', reminder['id']) \
                            .execute()
                        
                        logger.warning(f"Failed to send late reminder {reminder['id']}, attempt {retry_count}")
        
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