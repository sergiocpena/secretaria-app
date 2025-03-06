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
                                    self.logger.warning(f"Attempted to create reminder in the past: {reminder['title']} at {dt}")
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
                                    self.logger.info(f"Created reminder {reminder_id}: {reminder['title']} at {scheduled_time}")
                                else:
                                    self.logger.error(f"Failed to create reminder: {reminder['title']}")
                            except Exception as e:
                                self.logger.error(f"Error processing datetime: {str(e)}")

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

    def _parse_time(self, time_str, current_time=None):
        """
        Parse a time string into a datetime object.
        
        Args:
            time_str (str): The time string to parse
            current_time (datetime, optional): The current time context
            
        Returns:
            datetime: The parsed datetime object
        """
        try:
            self.logger.info(f"Parsing time: {time_str}")
            
            # Default to current time if not provided
            if not current_time:
                current_time = datetime.now(tz=BRAZIL_TZ)
            elif current_time.tzinfo is None:
                # Add timezone if missing
                current_time = BRAZIL_TZ.localize(current_time)
            
            # Handle common relative time expressions
            time_str = time_str.lower()
            
            # Handle "tomorrow" expressions
            if "tomorrow" in time_str:
                # Start with next day
                base_date = current_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                
                # Extract time if present (e.g., "tomorrow at 10:00 AM")
                time_match = re.search(r'(\d{1,2}):?(\d{2})?\s*(am|pm)?', time_str)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    
                    # Adjust for AM/PM
                    if time_match.group(3) == 'pm' and hour < 12:
                        hour += 12
                    elif time_match.group(3) == 'am' and hour == 12:
                        hour = 0
                    
                    return base_date.replace(hour=hour, minute=minute)
                else:
                    # Default to 9:00 AM if no specific time
                    return base_date.replace(hour=9, minute=0)
            
            # Handle "in X hours/minutes" or "daqui X horas/minutos" expressions
            hours_match = re.search(r'(\d+)\s*h(ora)?s?', time_str)
            minutes_match = re.search(r'(\d+)\s*min(uto)?s?', time_str)
            
            if "daqui" in time_str or "in" in time_str:
                delta = timedelta()
                
                if hours_match:
                    hours = int(hours_match.group(1))
                    delta += timedelta(hours=hours)
                    
                if minutes_match:
                    minutes = int(minutes_match.group(1))
                    delta += timedelta(minutes=minutes)
                    
                if delta.total_seconds() > 0:
                    return current_time + delta
            
            # Handle specific date formats
            date_formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%d/%m/%Y %H:%M",
                "%b/%d/%Y %H:%M",
                "%B %d, %Y %H:%M",
                "%Y-%m-%dT%H:%M:%S",
            ]
            
            for fmt in date_formats:
                try:
                    dt = datetime.strptime(time_str, fmt)
                    # Add timezone if missing
                    if dt.tzinfo is None:
                        dt = BRAZIL_TZ.localize(dt)
                    return dt
                except ValueError:
                    continue
                
            # Try dateutil parser as a fallback
            try:
                dt = parser.parse(time_str, fuzzy=True)
                # Add timezone if missing
                if dt.tzinfo is None:
                    dt = BRAZIL_TZ.localize(dt)
                return dt
            except Exception as e:
                self.logger.warning(f"Failed to parse with dateutil parser: {e}")
            
            # Handle day of month (e.g., "dia 7 as 8h")
            day_match = re.search(r'dia\s+(\d{1,2})', time_str)
            if day_match:
                day = int(day_match.group(1))
                
                # Start with current month and year
                target_date = current_time.replace(day=1)
                
                # If the day has already passed this month, move to next month
                if day < current_time.day:
                    target_date = target_date.replace(month=target_date.month % 12 + 1)
                    if target_date.month == 1:
                        target_date = target_date.replace(year=target_date.year + 1)
                
                # Set the day
                target_date = target_date.replace(day=day)
                
                # Extract time if present
                time_match = re.search(r'(\d{1,2}):?(\d{2})?\s*(am|pm|h)?', time_str)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    
                    # Adjust for AM/PM
                    if time_match.group(3) == 'pm' and hour < 12:
                        hour += 12
                    elif time_match.group(3) == 'am' and hour == 12:
                        hour = 0
                    
                    return target_date.replace(hour=hour, minute=minute)
                else:
                    # Default to 9:00 AM if no specific time
                    return target_date.replace(hour=9, minute=0)
            
            # If all parsing methods fail
            raise ValueError(f"Could not parse time string: {time_str}")
            
        except Exception as e:
            self.logger.error(f"Error parsing time {time_str}: {e}")
            raise
    
    def _parse_with_llm(self, message, current_time=None):
        """
        Parse a reminder message with LLM.
        
        Args:
            message (str): The reminder message to parse
            current_time (datetime, optional): The current time context
            
        Returns:
            dict: The parsed reminder data
        """
        try:
            # Format the current time for the prompt
            current_time_str = "not specified"
            if current_time:
                # Ensure current_time has timezone
                if current_time.tzinfo is None:
                    current_time = BRAZIL_TZ.localize(current_time)
                current_time_str = current_time.strftime("%b/%d/%Y %H:%M %Z")
                
            # Build prompt
            prompt = f"""
            You are a reminder parsing assistant. Extract the following information from the message:
            1. Title of the reminder (what the user wants to be reminded about)
            2. Scheduled time (when to remind them)

            Message: {message}
            Current time: {current_time_str}

            Parse the message and return a JSON object with the following structure:
            {{
                "title": "Title of the reminder",
                "parsed_time": "Specific date and time"
            }}

            If there are multiple reminders, include them in a 'reminders' array with the same structure.
            """
            
            # Use the chat_completion function that's already imported
            content = chat_completion(
                messages=[
                    {"role": "system", "content": "You are a reminder parsing assistant."},
                    {"role": "user", "content": prompt}
                ],
                model="gpt-3.5-turbo",
                temperature=0.1
            )
            
            # Parse the JSON from the response
            # Extract the JSON part from the response
            result = self._extract_json_from_response(content)
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error in _parse_with_llm: {e}")
            return None
        
    def _call_llm(self, prompt):
        """Call the LLM with the given prompt using our custom LLM utilities"""
        try:
            # Import the LLM utilities from your project
            from utils.llm_utils import get_completion
            
            # Call the LLM using your utility function
            content = get_completion(prompt)
            
            self.logger.info(f"LLM response received: {content[:100]}...")
            return content
            
        except Exception as e:
            self.logger.error(f"Error calling LLM API: {e}")
            # Don't use fallbacks, just propagate the error
            raise
        
    def _extract_json_from_response(self, response):
        """Extract JSON from LLM response"""
        try:
            # Try to parse the entire response as JSON
            return json.loads(response)
        except json.JSONDecodeError:
            # If that fails, try to find JSON block in text
            try:
                # Look for JSON block between ``` markers
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
                if json_match:
                    json_str = json_match.group(1)
                    return json.loads(json_str)
                
                # Look for JSON block between { and }
                json_match = re.search(r'({[\s\S]*})', response)
                if json_match:
                    json_str = json_match.group(1)
                    return json.loads(json_str)
            except Exception as e:
                self.logger.error(f"Error extracting JSON from response: {e}")
            
            self.logger.error("Could not extract valid JSON from response")
            return None

    def parse_reminder(self, message, action_type=None):
        """
        Parse a reminder message and extract the relevant details.
        
        Args:
            message (str): The reminder message to parse
            action_type (dict, optional): Additional context, such as current_time
            
        Returns:
            dict: The parsed reminder with title and scheduled_time
            
        Raises:
            ValueError: If parsing fails or required data is missing
            Exception: If any other error occurs during parsing
        """
        self.logger.info(f"STARTING parse_reminder with message: {message[:50]}...")
        
        # Extract current time context if provided
        current_time = None
        if action_type and 'current_time' in action_type:
            current_time_str = action_type['current_time']
            # Clean up the time string and parse it
            current_time_str = re.sub(r'[^\w\s:/\-]', '', current_time_str)
            self.logger.info(f"Current time string from action_type: '{current_time_str}'")
            self.logger.info(f"Cleaned current time string: '{current_time_str}'")
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
                error_msg = f"Failed to parse current time: {e}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
        # Attempt to parse the reminder with LLM
        self.logger.info("Attempting to parse with LLM")
        try:
            result = self._parse_with_llm(message, current_time)
            self.logger.info(f"Raw LLM result: {result}")
            
            if not result:
                error_msg = "LLM returned empty or None result"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
        except Exception as e:
            error_msg = f"Error in _parse_with_llm: {e}"
            self.logger.error(error_msg)
            raise
        
        try:
            # If we have reminders array and it's not empty, use the first one for title and time
            if 'reminders' in result and result['reminders'] and len(result['reminders']) > 0:
                first_reminder = result['reminders'][0]
                
                # Set the main title and time from the first reminder if not explicitly provided
                if 'title' not in result or not result['title']:
                    result['title'] = first_reminder.get('title', '')
                    self.logger.info(f"Used first reminder's title: {result['title']}")
                
                if 'parsed_time' not in result or not result['parsed_time']:
                    result['parsed_time'] = first_reminder.get('parsed_time', '')
                    self.logger.info(f"Used first reminder's parsed_time: {result['parsed_time']}")
            
            # Check if we have required title and time
            if 'title' not in result or not result['title']:
                error_msg = "LLM result missing title and unable to extract from reminders"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            if 'parsed_time' not in result or not result['parsed_time']:
                error_msg = "LLM result missing parsed_time and unable to extract from reminders"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
        
            self.logger.info(f"Final LLM result after extraction: {result}")
            
            # Convert parsed time string to datetime object
            reminder_data = result.copy()
            try:
                if 'parsed_time' in reminder_data and reminder_data['parsed_time']:
                    scheduled_time = self._parse_time(reminder_data['parsed_time'], current_time)
                    reminder_data['scheduled_time'] = scheduled_time
                    del reminder_data['parsed_time']  # Remove the intermediate parsed_time
                    self.logger.info(f"Converted main parsed_time to scheduled_time: {scheduled_time}")
            except Exception as e:
                error_msg = f"Failed to convert parsed time to datetime: {e}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            # Handle multiple reminders if present
            if 'reminders' in reminder_data and reminder_data['reminders']:
                self.logger.info(f"Processing {len(reminder_data['reminders'])} sub-reminders")
                for i, reminder in enumerate(reminder_data['reminders']):
                    self.logger.info(f"Processing sub-reminder {i}: {reminder}")
                    if 'parsed_time' in reminder and reminder['parsed_time']:
                        try:
                            scheduled_time = self._parse_time(reminder['parsed_time'], current_time)
                            reminder_data['reminders'][i]['scheduled_time'] = scheduled_time
                            del reminder_data['reminders'][i]['parsed_time']
                            self.logger.info(f"Converted sub-reminder {i} parsed_time to scheduled_time: {scheduled_time}")
                        except Exception as e:
                            error_msg = f"Failed to convert parsed time for reminder {i}: {e}"
                            self.logger.error(error_msg)
                            raise ValueError(error_msg)
            
            self.logger.info(f"Reminder data after all parsing: {reminder_data}")
            
            # Convert datetime objects to ISO format strings
            try:
                reminder_data = self._convert_datetimes_to_iso(reminder_data)
                self.logger.info(f"Reminder data after datetime conversion: {reminder_data}")
            except Exception as e:
                error_msg = f"Error converting datetimes to ISO: {e}"
                self.logger.error(error_msg)
                raise
            
            self.logger.info(f"FINAL RESULT BEING RETURNED: {reminder_data}")
            return reminder_data
        except Exception as e:
            error_msg = f"Unexpected error in parse_reminder: {e}"
            self.logger.error(error_msg)
            # Re-raise the exception, don't return None
            raise

    def _convert_datetimes_to_iso(self, data):
        """
        Convert all datetime objects in data to ISO format strings
        Only used as fallback if utils.datetime_utils is not available
        """
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, datetime):
                    data[key] = value.isoformat()
                elif isinstance(value, (dict, list)):
                    data[key] = self._convert_datetimes_to_iso(value)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, datetime):
                    data[i] = item.isoformat()
                elif isinstance(item, (dict, list)):
                    data[i] = self._convert_datetimes_to_iso(item)
        return data

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
                llm_result = self._parse_with_llm(message, self.current_time)
                self.logger.info(f"LLM parsing result with custom time: {llm_result}")
                return llm_result
            except Exception as e:
                self.logger.error(f"Error in TimeAwareReminderAgent parse_reminder: {str(e)}")
                return None
        else:
            # Fall back to the parent implementation if no custom time
            return super().parse_reminder(message, action_type)
