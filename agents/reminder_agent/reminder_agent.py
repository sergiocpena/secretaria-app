import os
import json
import logging
import pytz
from datetime import datetime, timezone, timedelta

# Import from our modules
from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder,
    format_reminder_list_by_time, format_created_reminders,
    format_datetime, BRAZIL_TIMEZONE
)

logger = logging.getLogger(__name__)

class ReminderAgent:
    def __init__(self, send_message_func=None, intent_classifier=None):
        """Initialize the ReminderAgent with necessary dependencies"""
        self.send_message_func = send_message_func
        self.intent_classifier = intent_classifier
    
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

    def parse_reminder(self, message, action):
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
                
                IMPORTANTE SOBRE EXPRESSÕES TEMPORAIS RELATIVAS:
                - "daqui X minutos/horas/dias" SEMPRE significa X minutos/horas/dias a partir do momento atual
                - "daqui 2h" significa exatamente 2 horas a partir de agora, HOJE {current_day}/{current_month}/{current_year}
                - "amanhã" significa o dia seguinte
                - "hoje" significa o dia atual
                
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
            import time as time_module
            import openai
            
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
                                    dt_components["day"] = dt.day
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
            logger.error(f"Error processing reminder: {str(e)}")
            return "❌ Não consegui processar o lembrete. Por favor, tente novamente."

    def parse_datetime_with_llm(self, date_str):
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
            import openai
            
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
                
                # Add timezone info
                dt = brazil_tz.localize(dt)
                
                # Check if this was a relative time expression
                is_relative = parsed.get('relative', False)
                
                # For relative time expressions, we should never adjust the date further
                # as the LLM has already calculated the correct target time
                if not is_relative:
                    # Only adjust non-relative times if they're in the past
                    if dt < now_local:
                        # If it's today but earlier time, move to tomorrow
                        if dt.date() == now_local.date():
                            dt = dt + timedelta(days=1)
                            logger.info(f"Adjusted past time to tomorrow: {dt}")
                else:
                    logger.info(f"Keeping relative time as is: {dt}")
                
                # Convert to UTC for storage
                utc_dt = dt.astimezone(timezone.utc)
                logger.info(f"Final parsed datetime (UTC): {utc_dt}")
                return utc_dt
                
            except Exception as e:
                logger.error(f"Error creating datetime object: {str(e)}")
                # Fallback to tomorrow noon
                tomorrow_noon = (now_local + timedelta(days=1)).replace(
                    hour=12, minute=0, second=0, microsecond=0
                )
                return tomorrow_noon.astimezone(timezone.utc)
        
        except Exception as e:
            logger.error(f"Error parsing datetime with LLM: {str(e)}")
            # Fallback to tomorrow noon
            brazil_tz = pytz.timezone('America/Sao_Paulo')
            now_local = datetime.now(brazil_tz)
            tomorrow_noon = (now_local + timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            return tomorrow_noon.astimezone(timezone.utc)

    def send_reminder_notification(self, reminder):
        """Envia uma notificação de lembrete para o usuário"""
        try:
            user_phone = reminder['user_phone']
            title = reminder['title']
            
            # Format the message
            message_body = f"🔔 *LEMBRETE*: {title}"
            
            logger.info(f"Sending reminder to {user_phone}: {message_body}")
            
            # Use the send_message_func if provided
            if self.send_message_func:
                success = self.send_message_func(user_phone, message_body)
                
                if success:
                    # Store in conversation history
                    from agents.general_agent.general_db import store_conversation
                    store_conversation(user_phone, message_body, 'text', False, agent="REMINDER")
                    return True
                else:
                    logger.error(f"Failed to send reminder to {user_phone}")
                    return False
            else:
                logger.error("No send_message_func provided to ReminderAgent")
                return False
                
        except Exception as e:
            logger.error(f"Error sending reminder notification: {str(e)}")
            return False

    def check_and_send_reminders(self):
        """Checks for pending reminders and sends notifications"""
        try:
            logger.info("Checking for pending reminders...")
            
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
                success = self.send_reminder_notification(reminder)
                
                if success:
                    # Mark as inactive
                    from utils.database import supabase
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
            logger.error(f"Error checking reminders: {str(e)}")
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
                logger.info(f"Found {len(late_reminders)} late reminders")
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

    def start_reminder_checker(self):
        """Inicia o verificador de lembretes em uma thread separada como backup"""
        def reminder_checker_thread():
            logger.info("Backup reminder checker thread started")
            
            # Configurar o intervalo de verificação (mais longo, já que temos o cron-job.org)
            check_interval = 300  # 5 minutos
            
            while True:
                try:
                    # Dormir primeiro
                    import time as time_module
                    time_module.sleep(check_interval)
                    
                    # Depois verificar os lembretes
                    logger.info("Running backup reminder check")
                    self.check_and_send_reminders()
                    
                except Exception as e:
                    logger.error(f"Error in backup reminder checker: {str(e)}")
        
        import threading
        thread = threading.Thread(target=reminder_checker_thread, daemon=True)
        thread.start()
        logger.info("Backup reminder checker background thread started")
        return thread
