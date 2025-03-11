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

# Create a prefixed logger class
class PrefixedLogger:
    def __init__(self, logger, prefix):
        self.logger = logger
        self.prefix = prefix
        
    def info(self, message, *args, **kwargs):
        self.logger.info(f"{self.prefix} {message}", *args, **kwargs)
        
    def warning(self, message, *args, **kwargs):
        self.logger.warning(f"{self.prefix} {message}", *args, **kwargs)
        
    def error(self, message, *args, **kwargs):
        self.logger.error(f"{self.prefix} {message}", *args, **kwargs)
    
    def debug(self, message, *args, **kwargs):
        self.logger.debug(f"{self.prefix} {message}", *args, **kwargs)
        
    def critical(self, message, *args, **kwargs):
        self.logger.critical(f"{self.prefix} {message}", *args, **kwargs)

class ReminderAgent:
    def __init__(self, send_message_func=None):
        """
        Initialize the ReminderAgent.
        
        Args:
            send_message_func (callable, optional): Function to send messages back to the user
        """
        base_logger = logging.getLogger(__name__)
        self.logger = PrefixedLogger(base_logger, "ReminderAgent:")
        self.send_message_func = send_message_func
        
        # Rest of initialization code
        self.logger.info("ReminderAgent initialized")
        
    def handle_reminder_intent(self, text, intent=None, action_type=None, user_id=None):
        """
        Handle a reminder intent
        
        Args:
            text (str): The reminder text
            intent (str, optional): The specific reminder intent (e.g., 'criar', 'listar', 'deletar')
            action_type (dict, optional): Additional context for the action
            user_id (str, optional): The user ID
            
        Returns:
            tuple: (success, response_message)
        """
        normalized_text = text.lower().strip()
        self.logger.info(f"Processing reminder intent with normalized text: '{normalized_text}'")
        
        # Default to 'create' intent if not specified
        if not intent:
            intent = 'criar'
        
        if intent == 'criar':
            self.logger.info("Detected create reminder request")
            try:
                # Parse the reminder text to extract details
                reminder_data = self.parse_reminder(normalized_text, action_type)
                
                if not reminder_data:
                    self.logger.warning("parse_reminder returned None")
                    return False, "❌ Não consegui entender os detalhes do lembrete. Por favor, tente novamente."
                
                self.logger.info(f"Reminder data after parsing: {reminder_data}")
                
                # Handle reminders array if present
                if 'reminders' in reminder_data and isinstance(reminder_data['reminders'], list) and reminder_data['reminders']:
                    success_count = 0
                    failed_count = 0
                    
                    for reminder in reminder_data['reminders']:
                        try:
                            # Each reminder in the array should have title and scheduled_time
                            if 'title' not in reminder or 'scheduled_time' not in reminder:
                                self.logger.warning(f"Reminder missing required fields: {reminder}")
                                failed_count += 1
                                continue
                            
                            # Create the reminder
                            reminder_id, scheduled_iso = self.create_reminder(
                                title=reminder['title'],
                                scheduled_time=reminder['scheduled_time'],
                                user_id=user_id
                            )
                            
                            if reminder_id:
                                success_count += 1
                            else:
                                failed_count += 1
                        except Exception as e:
                            self.logger.error(f"Error creating individual reminder: {e}")
                            failed_count += 1
                    
                    if success_count > 0:
                        if failed_count > 0:
                            return True, f"✅ Criei {success_count} lembretes (falha em {failed_count})"
                        else:
                            return True, f"✅ Criei {success_count} lembretes com sucesso!"
                    else:
                        return False, "❌ Não consegui criar nenhum dos lembretes."
                
                # Handle single reminder case
                elif 'title' in reminder_data and 'scheduled_time' in reminder_data:
                    reminder_id, scheduled_iso = self.create_reminder(
                        title=reminder_data['title'],
                        scheduled_time=reminder_data['scheduled_time'],
                        user_id=user_id
                    )
                    
                    if reminder_id:
                        # Format time for display
                        try:
                            scheduled_dt = datetime.fromisoformat(scheduled_iso)
                            localized_time = scheduled_dt.astimezone(BRAZIL_TZ)
                            formatted_time = localized_time.strftime("%d/%m/%Y às %H:%M")
                            
                            return True, f"✅ Lembrete criado: '{reminder_data['title']}' para {formatted_time}"
                        except Exception as e:
                            self.logger.error(f"Error formatting confirmation message: {e}")
                            return True, f"✅ Lembrete criado: '{reminder_data['title']}'"
                    else:
                        self.logger.warning("Failed to create reminder in database")
                        return False, "❌ Não consegui salvar o lembrete. Por favor, tente novamente."
                else:
                    self.logger.warning(f"Reminder data missing required fields: {reminder_data}")
                    return False, "❌ Não consegui entender os detalhes do lembrete. Por favor, tente novamente."
                
            except Exception as e:
                self.logger.error(f"Error in handle_reminder_intent: {str(e)}", exc_info=True)
                return False, "❌ Ocorreu um erro ao processar seu lembrete. Por favor, tente novamente."
        
        elif intent == 'listar':
            self.logger.info("Detected list reminders request")
            # Implementation for listing reminders
            try:
                reminders = self.list_reminders(user_id)
                if not reminders:
                    return True, "Você não tem lembretes ativos no momento."
                
                response = "Seus lembretes:\n\n"
                for i, reminder in enumerate(reminders, 1):
                    response += f"{i}. {reminder['title']} - {reminder['scheduled_time']}\n"
                
                return True, response
            except Exception as e:
                self.logger.error(f"Error listing reminders: {e}")
                return False, "❌ Não consegui listar seus lembretes. Por favor, tente novamente."
        
        elif intent == 'deletar':
            self.logger.info("Detected delete reminder request")
            # Implementation for deleting reminders
            try:
                # Logic to identify which reminder to delete
                reminder_id = None  # Extract from text
                if reminder_id:
                    success = self.delete_reminder(reminder_id, user_id)
                    if success:
                        return True, "✅ Lembrete excluído com sucesso!"
                    else:
                        return False, "❌ Não consegui encontrar esse lembrete para excluir."
                else:
                    return False, "❌ Por favor, especifique qual lembrete deseja excluir."
            except Exception as e:
                self.logger.error(f"Error deleting reminder: {e}")
                return False, "❌ Erro ao excluir o lembrete. Por favor, tente novamente."
        
        else:
            self.logger.warning(f"Unknown reminder intent: {intent}")
            return False, "❌ Não entendi o que você quer fazer com os lembretes. Você pode criar, listar ou excluir lembretes."

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
