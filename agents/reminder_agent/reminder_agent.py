import json
from datetime import datetime, timezone, timedelta
import pytz
import os
import re
import logging
from utils.datetime_utils import format_time_exact
from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder,
    format_reminder_list_by_time, format_created_reminders,
    format_datetime, BRAZIL_TIMEZONE
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ReminderAgent")

class ReminderAgent:
    def __init__(self):
        """
        Initialize the reminder agent
        """
        self.timezone = pytz.timezone('America/Sao_Paulo')
        
        # Initialize client only if API key is available
        self.client = None
        
        # Try different ways to get the API key with debugging
        api_key = None
        
        # Method 1: Using os.getenv
        api_key_getenv = os.getenv("OPENAI_API_KEY")
        logger.info(f"API key from os.getenv: {'FOUND' if api_key_getenv else 'NOT FOUND'}")
        
        # Method 2: Using os.environ.get
        api_key_environ = os.environ.get("OPENAI_API_KEY")
        logger.info(f"API key from os.environ.get: {'FOUND' if api_key_environ else 'NOT FOUND'}")
        
        # Method 3: Direct dictionary access
        try:
            api_key_direct = os.environ["OPENAI_API_KEY"]
            logger.info(f"API key from direct access: {'FOUND' if api_key_direct else 'NOT FOUND'}")
        except KeyError:
            api_key_direct = None
            logger.info("API key not found via direct access")
        
        # Try to load from dotenv file explicitly
        try:
            from dotenv import load_dotenv
            load_dotenv()
            logger.info("Attempted to load .env file explicitly")
            # Check again after loading dotenv
            api_key_after_dotenv = os.getenv("OPENAI_API_KEY")
            logger.info(f"API key after loading dotenv: {'FOUND' if api_key_after_dotenv else 'NOT FOUND'}")
        except ImportError:
            logger.warning("python-dotenv not installed, skipping explicit .env loading")
        
        # Use any method that worked
        api_key = api_key_getenv or api_key_environ or api_key_direct or api_key_after_dotenv
        
        if api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=api_key)
                logger.info("OpenAI client initialized successfully")
            except ImportError:
                logger.error("OpenAI package not installed.")
            except Exception as e:
                logger.error(f"Error initializing OpenAI client: {str(e)}.")
        else:
            logger.error("No OpenAI API key found. Cannot parse reminder without LLM.")
            raise ValueError("OpenAI client is not available. Cannot parse reminder without LLM.")
    
    def handle_reminder_intent(self, user_phone, message_text):
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
                cancel_data = self.parse_reminder(normalized_text, "cancelar")
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
                reminder_data = self.parse_reminder(normalized_text, "criar")
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

    def parse_reminder(self, message, action_type):
        """Parse a reminder message to extract title and time"""
        logger.info(f"Parsing reminder: '{message}' with action_type: {action_type}")
        
        # Parse the current_time if provided
        current_time = None
        if action_type and isinstance(action_type, dict) and 'current_time' in action_type:
            try:
                # Try to parse the current_time from the action_type
                current_time_str = action_type['current_time']
                logger.info(f"Current time string from action_type: '{current_time_str}'")
                
                # Remove timezone info if present
                date_part = current_time_str.replace(" BRT", "")
                logger.info(f"Cleaned current time string: '{date_part}'")
                
                current_time = datetime.strptime(date_part, "%b/%d/%Y %H:%M")
                current_time = self.timezone.localize(current_time)
                logger.info(f"Parsed current time: {current_time.isoformat()}")
            except (ValueError, TypeError) as e:
                logger.error(f"Error parsing current_time: {str(e)}. Using system time.")
        
        # If current_time couldn't be parsed, use system time
        if not current_time:
            current_time = datetime.now(self.timezone)
            logger.info(f"Using system time: {current_time.isoformat()}")
        
        
        # Use LLM-based parsing if available
        if self.client:
            try:
                logger.info("Attempting to parse with LLM")
                llm_result = self._parse_with_llm(message, current_time)
                logger.info(f"LLM parsing result: {llm_result}")
                
                # Process the result to ensure it has the expected format
                if "title" not in llm_result or not llm_result["title"]:
                    logger.warning("LLM result missing title, using fallback")
                    llm_result["title"] = "Lembrete"
                
                if "parsed_time" not in llm_result or not llm_result["parsed_time"]:
                    logger.warning("LLM result missing parsed_time, using fallback")
                    reminder_time = current_time + timedelta(minutes=30)
                    llm_result["parsed_time"] = format_time_exact(reminder_time)
                
                logger.info(f"Final LLM result: {llm_result}")
                return llm_result
            except Exception as e:
                logger.error(f"LLM parsing failed: {str(e)}. Using rule-based parsing.")
        else:
            logger.error("No OpenAI client available")
            raise ValueError("OpenAI client is not available. Cannot parse reminder without LLM.")
    
    def _parse_with_llm(self, message, current_time):
        """Parse a reminder message using OpenAI LLM"""
        prompt = f"""
        Current time: {current_time.isoformat()}
        Request: "{message}"
        
        Parse this reminder request in Portuguese. You MUST extract:
        1. The title of each reminder (this is REQUIRED and should never be empty)
        2. The exact time for each reminder (this is REQUIRED and should always be present)
        
        For multiple reminders in a single message, return an array of all reminders.
        
        Important instructions:
        - NEVER leave a reminder without a title
        - NEVER leave a reminder without a parsed_time
        - Always extract the actual title from the user's message
        - When Friday is mentioned, use the CORRECT date for the coming Friday

        Example 1:
        Current time: 2025-03-05T14:47:00
        Request: "Me lembra de pagar a babá daqui 2h"
        Expected output:
        {{
          "title": "pagar a babá",
          "parsed_time": "Mar/5/2025 16:47 BRT"
        }}
        
        Example 2:
        Current time: 2025-03-05T14:47:00
        Request: "Daqui 30 minutos me lembra de tirar a roupa da máquina"
        Expected output:
        {{
          "title": "tirar a roupa da máquina",
          "parsed_time": "Mar/5/2025 15:17 BRT"
        }}
        
        Example 3:
        Current time: 2025-03-05T14:47:00
        Request: "Me lembra de ligar para o médico amanhã às 10h"
        Expected output:
        {{
          "title": "ligar para o médico",
          "parsed_time": "Mar/6/2025 10:00 BRT"
        }}
        
        Example 4:
        Current time: 2025-03-05T14:47:00
        Request: "Me lembra de:
        -pagar a conta de luz amanhã as 10
        -levar o cachorro no pet shop na sexta as 8
        -ir na padaria daqui 3h"
        Expected output:
        {{
          "reminders": [
            {{
              "title": "pagar a conta de luz",
              "parsed_time": "Mar/6/2025 10:00 BRT"
            }},
            {{
              "title": "levar o cachorro no pet shop",
              "parsed_time": "Mar/7/2025 08:00 BRT"
            }},
            {{
              "title": "ir na padaria",
              "parsed_time": "Mar/5/2025 17:47 BRT"
            }}
          ]
        }}
        
        Return your answer as a JSON object with these fields:
        - For single reminders: title and parsed_time
        - For multiple reminders: a reminders array with title and parsed_time for each
        The parsed_time should be formatted as "Mar/d/YYYY HH:MM BRT"
        """
        
        logger.info(f"Preparing LLM prompt with current_time: {current_time.isoformat()}")
        logger.info("Sending request to OpenAI API")
        
        # Make the API call
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a specialized reminder parsing assistant for Portuguese language. Your job is to extract reminder titles and times from natural language requests with perfect accuracy. You must ALWAYS include both title and parsed_time for every reminder. When Friday is mentioned, always use the correct date for the coming Friday."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        # Parse the response
        result = response.choices[0].message.content
        logger.info(f"Raw LLM response: {result}")
        
        try:
            parsed_data = json.loads(result)
            logger.info(f"Parsed LLM response: {parsed_data}")
            
            # Check if the response contains a reminders array for multiple reminders
            if "reminders" in parsed_data:
                # We have multiple reminders, validate each one
                for i, reminder in enumerate(parsed_data["reminders"]):
                    # Validate required fields
                    if "title" not in reminder or not reminder["title"]:
                        error_msg = f"Reminder {i+1} missing title field in LLM response"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                    
                    if "parsed_time" not in reminder or not reminder["parsed_time"]:
                        error_msg = f"Reminder {i+1} missing parsed_time field in LLM response"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                
                return parsed_data
            else:
                # Single reminder case
                # Validate required fields
                if "title" not in parsed_data or not parsed_data["title"]:
                    error_msg = "Missing title field in LLM response"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                if "parsed_time" not in parsed_data or not parsed_data["parsed_time"]:
                    error_msg = "Missing parsed_time field in LLM response"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                return parsed_data
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {str(e)}")
            raise ValueError(f"Failed to parse LLM response as JSON: {str(e)}")