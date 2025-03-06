import json
from datetime import datetime, timezone, timedelta
import pytz
import os
import re
import logging
from utils.datetime_utils import format_time_exact
from utils.llm_utils import get_openai_client, chat_completion, parse_json_response
from dateutil import parser
from pytz import timezone as pytz_timezone

# Import reminder DB functions
from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder,
    format_reminder_list_by_time, format_created_reminders,
    format_datetime, BRAZIL_TIMEZONE
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ReminderAgent")

# Get a consistent timezone to use throughout the agent
BRAZIL_TZ = pytz.timezone('America/Sao_Paulo')

class ReminderAgent:
    def __init__(self, send_message_func=None):
        """
        Initialize the ReminderAgent.
        
        Args:
            send_message_func (callable, optional): Function to send messages back to the user
        """
        self.logger = logging.getLogger(__name__)
        self.send_message_func = send_message_func
        
        # Rest of initialization code
        self.logger.info("ReminderAgent initialized")
        
    def handle_reminder_intent(self, user_phone, message_text):
        """Processa intenções relacionadas a lembretes"""
        try:
            # Normalizar o texto da mensagem
            if isinstance(message_text, (list, tuple)):
                message_text = ' '.join(str(x) for x in message_text)  # Convert list to string safely
            normalized_text = str(message_text).lower().strip()

            self.logger.info(f"Processing reminder intent with normalized text: '{normalized_text}'")

            # Special case for "cancelar todos os lembretes" - handle it directly
            if "cancelar todos" in normalized_text or "excluir todos" in normalized_text or "apagar todos" in normalized_text:
                self.logger.info("Detected cancel all reminders request")

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
                self.logger.info("Detected list reminders request")
                reminders = list_reminders(user_phone)
                self.logger.info(f"Found {len(reminders)} reminders to list")
                return format_reminder_list_by_time(reminders)

            # Verificar se é uma solicitação para cancelar lembretes
            cancel_keywords = ["cancelar", "remover", "apagar", "deletar", "excluir"]
            is_cancel_request = any(keyword in normalized_text for keyword in cancel_keywords)

            # Verificar se é uma solicitação para criar lembretes
            create_keywords = ["lembrar", "lembra", "lembre", "criar lembrete", "novo lembrete", "adicionar lembrete"]
            is_create_request = any(keyword in normalized_text for keyword in create_keywords)

            if is_cancel_request:
                self.logger.info("Detected cancel reminder request")

                # First, get the list of active reminders for this user
                reminders = list_reminders(user_phone)
                self.logger.info(f"Found {len(reminders)} active reminders for user {user_phone}")

                if not reminders:
                    return "Você não tem lembretes ativos para cancelar."

                # Parse the cancellation request
                cancel_data = self.parse_reminder(normalized_text, "cancelar")
                self.logger.info(f"Cancel data after parsing: {cancel_data}")

                if cancel_data and "cancel_type" in cancel_data:
                    cancel_type = cancel_data["cancel_type"]

                    # Handle different cancellation types
                    if cancel_type == "all":
                        self.logger.info("Cancelling all reminders")
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
                        self.logger.info(f"Cancelling reminders by numbers: {numbers}")

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
                        self.logger.info(f"Cancelling reminders in range: {range_start} to {range_end}")

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
                        self.logger.info(f"Cancelling reminders by title: {title}")

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
                self.logger.info("Detected create reminder request")
                # Parse the reminder data
                reminder_data = self.parse_reminder(normalized_text, "criar")
                self.logger.info(f"Reminder data after parsing: {reminder_data}")

                if reminder_data and "reminders" in reminder_data and reminder_data["reminders"]:
                    self.logger.info(f"Found {len(reminder_data['reminders'])} reminders in parsed data")
                    # Processar múltiplos lembretes
                    created_reminders = []
                    invalid_reminders = []

                    for reminder in reminder_data["reminders"]:
                        self.logger.info(f"Processing reminder: {reminder}")
                        
                        # Check if the reminder has both title and a time (either scheduled_time or datetime)
                        if "title" in reminder and ("scheduled_time" in reminder or "datetime" in reminder):
                            try:
                                # Create datetime object based on the format we have
                                if "scheduled_time" in reminder:
                                    # Parse ISO format string to datetime
                                    scheduled_time_str = reminder["scheduled_time"]
                                    self.logger.info(f"Parsing scheduled_time string: {scheduled_time_str}")
                                    
                                    # Try different parsing approaches
                                    try:
                                        # First try fromisoformat
                                        scheduled_time = datetime.fromisoformat(scheduled_time_str)
                                    except ValueError:
                                        # Fall back to dateutil.parser
                                        from dateutil import parser
                                        scheduled_time = parser.parse(scheduled_time_str)
                                    
                                    # Make sure it's localized 
                                    if scheduled_time.tzinfo is None:
                                        brazil_tz = pytz.timezone('America/Sao_Paulo')
                                        scheduled_time = brazil_tz.localize(scheduled_time)
                                    
                                    # Get current time for past check
                                    now_local = datetime.now(scheduled_time.tzinfo)
                                    
                                    self.logger.info(f"Parsed scheduled_time: {scheduled_time}")
                                elif "datetime" in reminder:
                                    # Original datetime components approach
                                    dt_components = reminder["datetime"]
                                    # ... existing datetime component code ...
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
                                    scheduled_time = brazil_tz.localize(naive_dt)
                                    
                                # Check if the reminder is in the past
                                if scheduled_time < now_local:
                                    self.logger.warning(f"Attempted to create reminder in the past: {reminder['title']} at {scheduled_time}")
                                    invalid_reminders.append({
                                        "title": reminder["title"],
                                        "time": scheduled_time,
                                        "reason": "past"
                                    })
                                    continue
                                
                                # Convert to UTC
                                if scheduled_time.tzinfo:
                                    scheduled_time = scheduled_time.astimezone(timezone.utc)
                                
                                # Create the reminder
                                reminder_id = create_reminder(user_phone, reminder["title"], scheduled_time)
                                
                                if reminder_id:
                                    created_reminders.append({
                                        "title": reminder["title"],
                                        "time": scheduled_time
                                    })
                                    self.logger.info(f"Created reminder {reminder_id}: {reminder['title']} at {scheduled_time}")
                                else:
                                    self.logger.error(f"Failed to create reminder: {reminder['title']}")
                            except Exception as e:
                                self.logger.error(f"Error processing reminder: {str(e)}", exc_info=True)
                                invalid_reminders.append({
                                    "title": reminder.get("title", "Unknown"),
                                    "reason": f"error: {str(e)}"
                                })
                        else:
                            self.logger.warning(f"Incomplete reminder data: {reminder}")
                            if "title" in reminder:
                                invalid_reminders.append({
                                    "title": reminder["title"],
                                    "reason": "missing_time"
                                })
                    
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
                        self.logger.warning("Failed to create any reminders")
                        return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
                else:
                    self.logger.warning("Failed to parse reminder creation request")
                    return "❌ Não consegui entender os detalhes do lembrete. Por favor, especifique o título e quando deseja ser lembrado."

            # If we get here, we couldn't determine the intent
            self.logger.warning(f"Could not determine specific reminder intent for: '{normalized_text}'")
            return "Não entendi o que você deseja fazer com os lembretes. Você pode criar, listar ou cancelar lembretes."

        except Exception as e:
            self.logger.error(f"Error in handle_reminder_intent: {str(e)}")
            return "Ocorreu um erro ao processar sua solicitação de lembrete. Por favor, tente novamente."

    def process_reminder(self, user_phone, title, time_str):
        """Processa a criação de um novo lembrete"""
        try:
            # Converter data/hora para timestamp
            scheduled_time = self.parse_datetime_with_llm(time_str)

            # Criar o lembrete
            reminder_id = create_reminder(user_phone, title, scheduled_time)

            if reminder_id:
                return f"✅ Lembrete criado: {title} para {format_datetime(scheduled_time)}"
            else:
                return "❌ Não consegui criar o lembrete. Por favor, tente novamente."

        except Exception as e:
            self.logger.error(f"Error processing reminder: {str(e)}")
            return "❌ Não consegui processar o lembrete. Por favor, tente novamente."

    def start_reminder_checker(self):
        """Inicia o verificador de lembretes em uma thread separada como backup"""
        def reminder_checker_thread():
            self.logger.info("Backup reminder checker thread started")

            # Configurar o intervalo de verificação (mais longo, já que temos o cron-job.org)
            check_interval = 300  # 5 minutos

            while True:
                try:
                    # Dormir primeiro
                    import time as time_module
                    time_module.sleep(check_interval)

                    # Depois verificar os lembretes
                    self.logger.info("Running backup reminder check")
                    self.check_and_send_reminders()

                except Exception as e:
                    self.logger.error(f"Error in backup reminder checker: {str(e)}")

        import threading
        thread = threading.Thread(target=reminder_checker_thread, daemon=True)
        thread.start()
        self.logger.info("Backup reminder checker background thread started")
        return thread

    def check_and_send_reminders(self):
        """Checks for pending reminders and sends notifications"""
        try:
            self.logger.info("Checking for pending reminders...")

            # Get current time in UTC
            now = datetime.now(timezone.utc)
            # Truncate seconds for comparison
            now_truncated = now.replace(second=0, microsecond=0)

            # Get all active reminders
            from utils.database import supabase
            result = supabase.table('reminders') \
                .select('*') \
                .eq('is_active', True) \
                .execute()

            reminders = result.data
            self.logger.info(f"Found {len(reminders)} active reminders")

            # Filter reminders manually to ignore seconds
            pending_reminders = []
            for reminder in reminders:
                scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
                # Truncate seconds for comparison
                scheduled_time_truncated = scheduled_time.replace(second=0, microsecond=0)

                # Only include reminders that are due (current time >= scheduled time)
                # Add a debug log to see the comparison
                self.logger.info(f"Comparing reminder time {scheduled_time_truncated} with current time {now_truncated}")
                if now_truncated >= scheduled_time_truncated:
                    self.logger.info(f"Reminder {reminder['id']} is due")
                    pending_reminders.append(reminder)
                else:
                    self.logger.info(f"Reminder {reminder['id']} is not yet due")

            self.logger.info(f"Found {len(pending_reminders)} pending reminders after time comparison")

            sent_count = 0
            failed_count = 0

            for reminder in pending_reminders:
                # Send notification
                success = self.send_reminder_notification(reminder)

                if success:
                    # Mark as inactive
                    from utils.database import supabase
                    update_result = supabase.table('reminders') \
                        .update({'is_active': False}) \
                        .eq('id', reminder['id']) \
                        .execute()

                    self.logger.info(f"Reminder {reminder['id']} marked as inactive after sending")
                    sent_count += 1
                else:
                    self.logger.warning(f"Failed to send reminder {reminder['id']}, will try again later")
                    failed_count += 1

            # Also check late reminders
            late_results = self.check_late_reminders()

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
            self.logger.error(f"Error checking reminders: {str(e)}")
            return {"error": str(e)}

    def check_late_reminders(self):
        """Verifica lembretes atrasados que não foram enviados após várias tentativas"""
        try:
            # Obter a data e hora atual em UTC
            now = datetime.now(timezone.utc)
            # Truncate seconds
            now_truncated = now.replace(second=0, microsecond=0)

            # Definir um limite de tempo para considerar um lembrete como atrasado (ex: 30 minutos)
            late_threshold = now_truncated - timedelta(minutes=30)

            # Buscar lembretes ativos
            from utils.database import supabase
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
                self.logger.info(f"Found {len(late_reminders)} late reminders")
                processed = len(late_reminders)

                for reminder in late_reminders:
                    # Tentar enviar o lembrete atrasado
                    success = self.send_reminder_notification(reminder)

                    if success:
                        # Mark as inactive
                        update_result = supabase.table('reminders') \
                            .update({'is_active': False}) \
                            .eq('id', reminder['id']) \
                            .execute()

                        self.logger.info(f"Late reminder {reminder['id']} marked as inactive after sending")
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

                            self.logger.warning(f"Deactivated very late reminder {reminder['id']} (over 2 hours late)")
                            deactivated += 1
                        else:
                            self.logger.warning(f"Failed to send late reminder {reminder['id']}, will try again later")
                            failed += 1

            return {
                "processed": processed,
                "sent": sent,
                "failed": failed,
                "deactivated": deactivated
            }

        except Exception as e:
            self.logger.error(f"Error checking late reminders: {str(e)}")
            return {
                "processed": 0,
                "sent": 0,
                "failed": 0,
                "deactivated": 0,
                "error": str(e)
            }

    def send_reminder_notification(self, reminder):
        """Envia uma notificação de lembrete para o usuário"""
        try:
            user_phone = reminder['user_phone']
            title = reminder['title']

            # Format the message
            message_body = f"🔔 *LEMBRETE*: {title}"

            self.logger.info(f"Sending reminder to {user_phone}: {message_body}")

            # Use the send_message_func if provided
            if self.send_message_func:
                success = self.send_message_func(user_phone, message_body)

                if success:
                    # Store in conversation history
                    from agents.general_agent.general_db import store_conversation
                    store_conversation(user_phone, message_body, 'text', False, agent="REMINDER")
                    return True
                else:
                    self.logger.error(f"Failed to send reminder to {user_phone}")
                    return False
            else:
                self.logger.error("No send_message_func provided to ReminderAgent")
                return False

        except Exception as e:
            self.logger.error(f"Error sending reminder notification: {str(e)}")
            return False

    def parse_reminder(self, message, action_type=None):
        """
        Parse a reminder message and extract the relevant details.
        
        Args:
            message (str): The reminder message to parse
            action_type (dict, optional): Additional context, such as current_time
            
        Returns:
            dict: The parsed reminder with title and scheduled_time
        """
        # Import required modules
        import re
        import os
        import json
        from datetime import datetime, timedelta
        from dateutil import parser
        from openai import OpenAI
        
        self.logger.info(f"STARTING parse_reminder with message: {message[:50]}...")
        
        # Extract current time context if provided
        current_time = None
        if action_type and isinstance(action_type, dict) and 'current_time' in action_type:
            current_time_str = action_type['current_time']
            # Clean up the time string and parse it
            current_time_str = re.sub(r'[^\w\s:/\-]', '', current_time_str)
            self.logger.info(f"Current time string from action_type: '{current_time_str}'")
            
            try:
                # Try various datetime formats
                for fmt in ['%b/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S%z', '%Y-%m-%dT%H:%M:%S%z']:
                    try:
                        current_time = datetime.strptime(current_time_str, fmt)
                        break
                    except ValueError:
                        continue
                
                if not current_time:
                    # Try another approach with dateutil parser
                    current_time = parser.parse(current_time_str)
                    
                # Add timezone if missing
                if current_time.tzinfo is None:
                    current_time = BRAZIL_TZ.localize(current_time)
                    
                self.logger.info(f"Parsed current time: {current_time}")
            except Exception as e:
                self.logger.error(f"Failed to parse current time: {e}")
                # Default to now
                current_time = datetime.now(BRAZIL_TZ)
        else:
            # Default to now
            current_time = datetime.now(BRAZIL_TZ)
        
        # Attempt to parse the reminder with direct OpenAI call
        self.logger.info("Attempting to parse with direct OpenAI API call")
        try:        
            # Get API key
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY environment variable not set")
            
            # Create client
            client = OpenAI(api_key=api_key)
            
            # Prepare system prompt
            system_prompt = f"""You are a helpful AI assistant specialized in parsing reminder text into structured data.
Extract the reminder title and time information from the user's message.

Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S %z')}
Current year: {current_time.year}
Tomorrow's date: {(current_time + timedelta(days=1)).strftime('%Y-%m-%d')}

Your response should follow this format:
{{
  "title": "extracted reminder title",
  "scheduled_time": "YYYY-MM-DDTHH:MM:SS-03:00" (in ISO 8601 format, Brazil/São Paulo timezone)
}}

If there are multiple reminders, include them in a "reminders" array using the same format.
Always convert human-readable time expressions to proper ISO timestamps.
Never generate dates in the past.
"""
            
            # Prepare messages
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ]
            
            # Make the direct API call
            self.logger.info("Making direct OpenAI API call")
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0
            )
            
            # Process the response
            if response and response.choices and response.choices[0].message.content:
                content = response.choices[0].message.content
                self.logger.info(f"Raw response from OpenAI: {content}")
                
                # Parse JSON
                try:
                    parsed_data = json.loads(content)
                    self.logger.info(f"Parsed JSON data: {parsed_data}")
                    
                    # Validate the data
                    if "title" not in parsed_data:
                        self.logger.warning("Missing 'title' in parsed data")
                        parsed_data["title"] = "Lembrete"
                    
                    if "scheduled_time" not in parsed_data:
                        self.logger.warning("Missing 'scheduled_time' in parsed data")
                        # Default to tomorrow at noon
                        tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                        parsed_data["scheduled_time"] = tomorrow_noon.isoformat()
                    else:
                        # Validate the scheduled time is not in the past
                        try:
                            scheduled_time = datetime.fromisoformat(parsed_data["scheduled_time"])
                            if scheduled_time < current_time:
                                self.logger.warning(f"Scheduled time {scheduled_time} is in the past, adjusting to tomorrow")
                                tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                                parsed_data["scheduled_time"] = tomorrow_noon.isoformat()
                        except (ValueError, TypeError):
                            self.logger.warning(f"Could not parse scheduled_time: {parsed_data['scheduled_time']}")
                            tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                            parsed_data["scheduled_time"] = tomorrow_noon.isoformat()
                    
                    # Also check reminders array if present
                    if "reminders" in parsed_data and isinstance(parsed_data["reminders"], list):
                        for i, reminder in enumerate(parsed_data["reminders"]):
                            if "title" not in reminder:
                                reminder["title"] = f"Lembrete {i+1}"
                            
                            if "scheduled_time" not in reminder:
                                tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                                reminder["scheduled_time"] = tomorrow_noon.isoformat()
                            else:
                                # Validate the scheduled time is not in the past
                                try:
                                    scheduled_time = datetime.fromisoformat(reminder["scheduled_time"])
                                    if scheduled_time < current_time:
                                        self.logger.warning(f"Reminder {i} scheduled time {scheduled_time} is in the past, adjusting to tomorrow")
                                        tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                                        reminder["scheduled_time"] = tomorrow_noon.isoformat()
                                except (ValueError, TypeError):
                                    self.logger.warning(f"Could not parse reminder {i} scheduled_time: {reminder['scheduled_time']}")
                                    tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
                                    reminder["scheduled_time"] = tomorrow_noon.isoformat()
                    
                    return parsed_data
                except json.JSONDecodeError:
                    # Try to extract JSON with regex if the response isn't pure JSON
                    json_match = re.search(r'({.*})', content, re.DOTALL)
                    if json_match:
                        try:
                            extracted_json = json_match.group(1)
                            parsed_data = json.loads(extracted_json)
                            self.logger.info(f"Extracted JSON from text: {parsed_data}")
                            return parsed_data
                        except json.JSONDecodeError:
                            self.logger.error("Failed to parse extracted JSON")
            
            # If we got here, something went wrong
            raise ValueError("Failed to get valid response from OpenAI API")
        except Exception as e:
            self.logger.error(f"Error in parse_reminder: {str(e)}", exc_info=True)
            # Create a basic structure with a default reminder for tomorrow
            tomorrow_noon = (current_time + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
            
            # Try to extract a meaningful title from the message
            title_match = re.search(r'(?:me\s+lembra\s+de|lembrar\s+de)\s+(.*?)(?:\s+(?:em|daqui|amanhã|hoje|às|as|\d+\s*min|\d+\s*hora|pela|no|na|ao|às|as))?(?=$|\s)', message.lower(), re.IGNORECASE)
            
            title = "Lembrete"
            if title_match:
                title = title_match.group(1).strip()
            
            return {
                "title": title,
                "scheduled_time": tomorrow_noon.isoformat()
            }

class TimeAwareReminderAgent(ReminderAgent):
    """
    A version of ReminderAgent that allows overriding the current time for testing purposes.
    This is primarily used for evaluation scripts.
    """
    def __init__(self, send_message_func=None, intent_classifier=None, api_key=None, current_time=None):
        super().__init__(send_message_func)
        self.current_time = current_time
        
    def parse_reminder(self, message, action_type):
        """Override parse_reminder to use the specified current_time"""
        self.logger.info(f"TimeAwareReminderAgent parsing reminder with custom time: {self.current_time}")
        
        # If we have a custom current_time, use it
        if self.current_time:
            # Make sure it's timezone-aware
            if not self.current_time.tzinfo:
                self.current_time = BRAZIL_TZ.localize(self.current_time)
            
            self.logger.info(f"Using custom time for parsing: {self.current_time.isoformat()}")
            
            # Call the LLM parser directly with our custom time
            try:
                llm_result = self._parse_with_llm_iso(message, self.current_time)
                self.logger.info(f"LLM parsing result with custom time: {llm_result}")
                return llm_result
            except Exception as e:
                self.logger.error(f"Error in TimeAwareReminderAgent parse_reminder: {str(e)}")
                return None
        else:
            # Fall back to the parent implementation if no custom time
            return super().parse_reminder(message, action_type)
