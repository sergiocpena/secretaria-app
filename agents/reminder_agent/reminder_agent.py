"""
Reminder agent implementation.
This file contains the ReminderAgent class for handling reminder-related functionality.
"""
import logging
import threading
import time
import json
import openai
from datetime import datetime, timedelta

from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder, 
    get_pending_reminders, get_late_reminders,
    format_reminder_list_by_time, format_created_reminders,
    to_local_timezone, to_utc_timezone, format_datetime,
    BRAZIL_TIMEZONE
)

logger = logging.getLogger(__name__)

# Add this function to replace parse_json_response
def parse_json_response(response_text):
    """Parse a JSON response from the LLM"""
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {str(e)}")
        return None

class ReminderAgent:
    """Agent for handling reminder-related functionality"""
    
    def __init__(self, send_message_func=None, check_interval=60):
        """Initialize the ReminderAgent"""
        self.send_message_func = send_message_func
        self.check_interval = check_interval
        self.stop_event = threading.Event()
    
    def extract_reminder_details(self, message):
        """
        Extract reminder details from a message using LLM.
        """
        try:
            logger.info(f"Extracting reminder details from message: '{message[:50]}...' (truncated)")
            
            system_prompt = """
            Você é um assistente especializado em extrair detalhes de lembretes de mensagens em português.
            
            Analise a mensagem do usuário e extraia as seguintes informações:
            - O que deve ser lembrado (texto)
            - Quando o lembrete deve ser enviado (data e hora)
            
            Retorne um JSON com o seguinte formato:
            {
              "reminder_text": "texto do lembrete",
              "reminder_time": "YYYY-MM-DD HH:MM",
              "confidence": 0.0 a 1.0
            }
            
            Onde:
            - "reminder_text": o texto do que deve ser lembrado
            - "reminder_time": a data e hora no formato YYYY-MM-DD HH:MM
            - "confidence": sua confiança na extração (0.0 a 1.0)
            
            Se não conseguir extrair alguma informação, retorne null para o campo correspondente.
            Se a mensagem não contiver um pedido de lembrete, retorne confidence: 0.0.
            
            Exemplos:
            - "me lembra de pagar a conta amanhã às 10h" → {"reminder_text": "pagar a conta", "reminder_time": "2023-05-11 10:00", "confidence": 0.9}
            - "me lembra da reunião dia 15/05 às 14h" → {"reminder_text": "reunião", "reminder_time": "2023-05-15 14:00", "confidence": 0.9}
            - "lembrete para ligar para o médico na segunda" → {"reminder_text": "ligar para o médico", "reminder_time": "2023-05-15 09:00", "confidence": 0.7}
            - "como está o tempo hoje?" → {"reminder_text": null, "reminder_time": null, "confidence": 0.0}
            
            Hoje é {current_date}.
            """
            
            # Format the current date
            current_date = datetime.now(BRAZIL_TIMEZONE).strftime("%Y-%m-%d")
            system_prompt = system_prompt.format(current_date=current_date)
            
            # Use the new OpenAI API format
            from openai import OpenAI
            client = OpenAI()
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            response_text = response.choices[0].message.content
            
            # Parse the JSON response
            result = parse_json_response(response_text)
            if not result:
                logger.error("Failed to parse LLM response as JSON")
                return None
            
            logger.info(f"Extracted reminder details: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error extracting reminder details: {str(e)}")
            return None
    
    def extract_reminder_cancellation(self, message):
        """
        Extract reminder cancellation details from a message using LLM.
        """
        try:
            logger.info(f"Extracting reminder cancellation from message: '{message[:50]}...' (truncated)")
            
            system_prompt = """
            Você é um assistente especializado em extrair detalhes de cancelamento de lembretes de mensagens em português.
            
            Analise a mensagem do usuário e determine se ele está tentando cancelar um lembrete.
            Se sim, extraia o número ou identificador do lembrete a ser cancelado.
            
            Retorne um JSON com o seguinte formato:
            {
              "is_cancellation": true/false,
              "reminder_id": número ou null,
              "confidence": 0.0 a 1.0
            }
            
            Onde:
            - "is_cancellation": true se a mensagem é um pedido de cancelamento, false caso contrário
            - "reminder_id": o número/id do lembrete a ser cancelado, ou null se não for especificado
            - "confidence": sua confiança na extração (0.0 a 1.0)
            
            Exemplos:
            - "cancelar lembrete 2" → {"is_cancellation": true, "reminder_id": 2, "confidence": 0.9}
            - "remover lembrete número 5" → {"is_cancellation": true, "reminder_id": 5, "confidence": 0.9}
            - "apagar o lembrete 1" → {"is_cancellation": true, "reminder_id": 1, "confidence": 0.9}
            - "cancelar todos os lembretes" → {"is_cancellation": true, "reminder_id": null, "confidence": 0.7}
            - "como está o tempo hoje?" → {"is_cancellation": false, "reminder_id": null, "confidence": 0.0}
            """
            
            # Use OpenAI API directly
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            response_text = response.choices[0].message.content
            
            # Parse the JSON response
            result = parse_json_response(response_text)
            if not result:
                logger.error("Failed to parse LLM response as JSON")
                return None
            
            logger.info(f"Extracted reminder cancellation details: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error extracting reminder cancellation: {str(e)}")
            return None
    
    def detect_reminder_list_request(self, message):
        """
        Detect if a message is requesting to list reminders.
        """
        try:
            logger.info(f"Detecting reminder list request from message: '{message[:50]}...' (truncated)")
            
            system_prompt = """
            Você é um assistente especializado em detectar pedidos de listagem de lembretes em mensagens em português.
            
            Analise a mensagem do usuário e determine se ele está pedindo para listar seus lembretes.
            
            Retorne um JSON com o seguinte formato:
            {
              "is_list_request": true/false,
              "confidence": 0.0 a 1.0
            }
            
            Onde:
            - "is_list_request": true se a mensagem é um pedido de listagem de lembretes, false caso contrário
            - "confidence": sua confiança na detecção (0.0 a 1.0)
            
            Exemplos:
            - "listar lembretes" → {"is_list_request": true, "confidence": 0.9}
            - "quais são meus lembretes?" → {"is_list_request": true, "confidence": 0.9}
            - "mostre meus lembretes" → {"is_list_request": true, "confidence": 0.9}
            - "lembretes" → {"is_list_request": true, "confidence": 0.7}
            - "como está o tempo hoje?" → {"is_list_request": false, "confidence": 0.0}
            """
            
            # Use OpenAI API directly
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            response_text = response.choices[0].message.content
            
            # Parse the JSON response
            result = parse_json_response(response_text)
            if not result:
                logger.error("Failed to parse LLM response as JSON")
                return None
            
            logger.info(f"Detected reminder list request: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error detecting reminder list request: {str(e)}")
            return None
    
    # Add any other methods that might use llm_utils here
    
    def handle_reminder_intent(self, from_number, message):
        """
        Handle a reminder intent from a user message.
        """
        try:
            # First check if it's a request to list reminders
            list_request = self.detect_reminder_list_request(message)
            if list_request and list_request.get('is_list_request', False) and list_request.get('confidence', 0) > 0.5:
                # List reminders
                reminders = list_reminders(from_number)
                if not reminders:
                    return "Você não tem nenhum lembrete agendado."
                
                formatted_list = format_reminder_list_by_time(reminders)
                return f"Seus lembretes:\n\n{formatted_list}"
            
            # Then check if it's a cancellation request
            cancel_request = self.extract_reminder_cancellation(message)
            if cancel_request and cancel_request.get('is_cancellation', False) and cancel_request.get('confidence', 0) > 0.5:
                reminder_id = cancel_request.get('reminder_id')
                if reminder_id is not None:
                    # Cancel a specific reminder
                    result = cancel_reminder(from_number, reminder_id)
                    if result:
                        return f"Lembrete {reminder_id} cancelado com sucesso."
                    else:
                        return f"Não encontrei um lembrete com o número {reminder_id}."
                else:
                    # User wants to cancel but didn't specify which one
                    reminders = list_reminders(from_number)
                    if not reminders:
                        return "Você não tem nenhum lembrete para cancelar."
                    
                    formatted_list = format_reminder_list_by_time(reminders)
                    return f"Qual lembrete você deseja cancelar? Por favor, especifique o número:\n\n{formatted_list}"
            
            # Finally, try to extract reminder details for creation
            reminder_details = self.extract_reminder_details(message)
            if reminder_details and reminder_details.get('confidence', 0) > 0.5:
                reminder_text = reminder_details.get('reminder_text')
                reminder_time_str = reminder_details.get('reminder_time')
                
                if not reminder_text or not reminder_time_str:
                    return "Não consegui entender todos os detalhes do lembrete. Por favor, especifique o que devo lembrar e quando."
                
                try:
                    # Parse the datetime
                    reminder_time = datetime.strptime(reminder_time_str, "%Y-%m-%d %H:%M")
                    reminder_time = BRAZIL_TIMEZONE.localize(reminder_time)
                    
                    # Create the reminder
                    reminder_id = create_reminder(from_number, reminder_text, reminder_time)
                    
                    # Format the confirmation message
                    local_time = format_datetime(reminder_time)
                    return f"Lembrete criado com sucesso! Vou te lembrar de '{reminder_text}' em {local_time}."
                    
                except ValueError:
                    return "Não consegui entender a data e hora do lembrete. Por favor, tente novamente com um formato como 'amanhã às 10h' ou '15/05 às 14h'."
            
            # If we got here, we couldn't handle the reminder intent
            return "Não consegui entender seu pedido de lembrete. Por favor, tente novamente com algo como 'me lembra de pagar a conta amanhã às 10h'."
            
        except Exception as e:
            logger.error(f"Error handling reminder intent: {str(e)}")
            return "Ocorreu um erro ao processar seu pedido de lembrete. Por favor, tente novamente."
    
    def check_and_send_reminders(self):
        """
        Check for pending reminders and send them.
        """
        try:
            logger.info("Checking for pending reminders")
            
            # Get pending reminders (due now)
            pending_reminders = get_pending_reminders()
            
            # Get late reminders (missed while the service was down)
            late_reminders = get_late_reminders()
            
            total_reminders = len(pending_reminders) + len(late_reminders)
            logger.info(f"Found {len(pending_reminders)} pending and {len(late_reminders)} late reminders")
            
            # Process pending reminders
            for reminder in pending_reminders:
                self._send_reminder(reminder, is_late=False)
            
            # Process late reminders
            for reminder in late_reminders:
                self._send_reminder(reminder, is_late=True)
            
            return {
                "status": "success",
                "pending_count": len(pending_reminders),
                "late_count": len(late_reminders),
                "total_processed": total_reminders
            }
            
        except Exception as e:
            logger.error(f"Error checking reminders: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _send_reminder(self, reminder, is_late=False):
        """
        Send a reminder to the user.
        """
        try:
            if not self.send_message_func:
                logger.error("No send_message_func provided to ReminderAgent")
                return False
            
            user_number = reminder['user_number']
            reminder_text = reminder['reminder_text']
            reminder_time = reminder['reminder_time']
            reminder_id = reminder['id']
            
            # Format the reminder message
            local_time = format_datetime(to_local_timezone(reminder_time))
            
            if is_late:
                message = f"⏰ LEMBRETE ATRASADO ⏰\n\n{reminder_text}\n\nEste lembrete estava agendado para {local_time}, mas não pude enviá-lo na hora."
            else:
                message = f"⏰ LEMBRETE ⏰\n\n{reminder_text}"
            
            # Send the message
            self.send_message_func(user_number, message)
            
            # Mark the reminder as sent in the database
            # This would typically update a 'sent' flag in your database
            # For now, we'll just log it
            logger.info(f"Sent reminder {reminder_id} to {user_number}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error sending reminder: {str(e)}")
            return False
    
    def _check_reminders_loop(self):
        """
        Background thread function to periodically check for reminders.
        """
        logger.info(f"Starting reminder checker loop with interval {self.check_interval}s")
        
        while not self.stop_event.is_set():
            try:
                self.check_and_send_reminders()
            except Exception as e:
                logger.error(f"Error in reminder checker loop: {str(e)}")
            
            # Sleep for the check interval, but check the stop event periodically
            for _ in range(self.check_interval):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
    
    def start_reminder_checker(self):
        """
        Start the background thread for checking reminders.
        """
        self.stop_event.clear()
        checker_thread = threading.Thread(target=self._check_reminders_loop, daemon=True)
        checker_thread.start()
        logger.info("Reminder checker thread started")
        return checker_thread
    
    def stop_reminder_checker(self):
        """
        Stop the background thread for checking reminders.
        """
        self.stop_event.set()
        logger.info("Reminder checker thread stopping")