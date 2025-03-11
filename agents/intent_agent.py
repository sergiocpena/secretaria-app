"""
Intent classification module.
This file contains the IntentClassifier class for detecting different types of intents in messages.
"""
import logging
import json
import openai
import time as time_module
from utils.llm_utils import chat_completion, parse_json_response

logger = logging.getLogger(__name__)

class IntentAgent:
    """
    Class for classifying the intent of user messages.
    Currently supports reminder intents, but can be extended for other types.
    """
    
    def __init__(self):
        """Initialize the IntentClassifier"""
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
            
            # Use the centralized LLM utility
            response_text = chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                model="gpt-4o-mini",
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            elapsed_time = time_module.time() - start_time
            
            if not response_text:
                logger.error("IntentAgent: Failed to get response from LLM")
                raise ValueError("IntentAgent: Failed to get response from LLM")
            
            # Parse the JSON response
            result = parse_json_response(response_text)
            if not result:
                logger.error("IntentAgent: Failed to parse LLM response as JSON")
                raise ValueError("IntentAgent: Failed to parse LLM response as JSON")
            
            logger.info(f"IntentAgent: LLM intent detection result: {result} (took {elapsed_time:.2f}s)")
            
            intent_type = result.get("intent_type")
            
            return intent_type
            
        except Exception as e:
            logger.error(f"IntentAgent: Error in LLM intent detection: {str(e)}")
            raise Exception(f"IntentAgent: Error in LLM intent detection: {str(e)}") 