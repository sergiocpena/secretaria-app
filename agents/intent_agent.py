"""
Intent classification module.
This file contains the IntentAgent class for detecting different types of intents in messages.
"""
import logging
import json
import openai
import time as time_module

logger = logging.getLogger(__name__)

class IntentAgent:
    """
    Class for classifying the intent of user messages.
    Currently supports reminder intents, but can be extended for other types.
    """
    
    def __init__(self):
        """Initialize the IntentAgent"""
        pass
    
    def detect_intent(self, message):
        """
        Main method to detect all types of intents in a message.
        Currently only detects reminder intents, but can be extended.
        
        Returns:
            tuple: (intent_type, intent_details)
                intent_type: str - The type of intent (e.g., "reminder", "general")
        """
        # Log the incoming message (truncated for privacy/brevity)
        truncated_message = message[:50] + "..." if len(message) > 50 else message
        logger.info(f"IntentAgent:Detecting intent for message: '{truncated_message}'")
        
        try:
            logger.info(f"IntentAgent: Detecting reminder intent with LLM for message: '{message[:50]}...' (truncated)")
            
            system_prompt = """
            Você é um assistente especializado em detectar intenções relacionadas a lembretes em mensagens em português.
            
            Analise a mensagem do usuário e identifique se ela contém uma intenção relacionada a lembretes.
            
            Retorne um JSON com o seguinte formato:
            {
              "intent_type": "reminder" ou "general"
            }
            
            Onde:
            - "reminder": indica que a mensagem contém uma intenção relacionada a lembretes
            - "general": indica que a mensagem NÂO contém uma intenção relacionada a lembretes
            
            Exemplos:
            - "me lembra de pagar a conta amanhã" → {"intent_type": "reminder"}
            - "meus lembretes" → {"intent_type": "reminder"}
            - "cancelar lembrete 2" → {"intent_type": "reminder"}
            - "lembrete" → {"intent_type": "reminder"}
            - "como está o tempo hoje?" → {"intent_type": "general"}
            - "quem descobriu o Brasil?" → {"intent_type": "general"}
            - "o que comer para ser saudável?" → {"intent_type": "general"}
            """
            
            start_time = time_module.time()
            
            # Use OpenAI's chat completion directly
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            elapsed_time = time_module.time() - start_time
            
            if not response.choices or not response.choices[0].message.content:
                logger.error("IntentAgent: Failed to get response from LLM")
                raise ValueError("IntentAgent: Failed to get response from LLM")
            
            response_text = response.choices[0].message.content
            
            # Parse the JSON response
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                logger.error("IntentAgent: Failed to parse LLM response as JSON")
                raise ValueError("IntentAgent: Failed to parse LLM response as JSON")
            
            logger.info(f"IntentAgent: LLM intent detection result: {result} (took {elapsed_time:.2f}s)")
            
            intent_type = result.get("intent_type")
            
            return intent_type, None  # Return both intent_type and intent_details (None for now)
            
        except Exception as e:
            logger.error(f"IntentAgent: Error in LLM intent detection: {str(e)}")
            raise Exception(f"IntentAgent: Error in LLM intent detection: {str(e)}")

    def detect_intent_with_llm(self, message):
        """Detect intent using LLM"""
        try:
            logger.info(f"IntentAgent: Detecting reminder intent with LLM for message: '{message[:20]}...' (truncated)")
            
            # Create the system prompt
            system_prompt = """
            Você é um assistente especializado em classificar a intenção de mensagens em português.
            
            Analise a mensagem do usuário e determine se é um pedido de:
            1. Criar um lembrete
            2. Listar lembretes existentes
            3. Cancelar um lembrete
            4. Conversa geral (qualquer outra coisa)
            
            Retorne um JSON com o seguinte formato:
            {
              "intent": "reminder_create | reminder_list | reminder_cancel | general",
              "confidence": 0.0 a 1.0
            }
            
            Onde:
            - "intent": o tipo de intenção detectada
            - "confidence": sua confiança na classificação (0.0 a 1.0)
            
            Exemplos:
            - "me lembra de pagar a conta amanhã" → {"intent": "reminder_create", "confidence": 0.9}
            - "quais são meus lembretes?" → {"intent": "reminder_list", "confidence": 0.9}
            - "cancelar lembrete 2" → {"intent": "reminder_cancel", "confidence": 0.9}
            - "como está o tempo hoje?" → {"intent": "general", "confidence": 0.9}
            """
            
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
            
            # Extract the content from the response
            response_text = response.choices[0].message.content
            
            # Parse the JSON response
            result = json.loads(response_text)
            
            logger.info(f"IntentAgent: LLM intent detection result: {result}")
            return result
            
        except Exception as e:
            logger.error(f"IntentAgent: Error in LLM intent detection: {str(e)}")
            # Return a default intent on error
            return {"intent": "general", "confidence": 0.5} 