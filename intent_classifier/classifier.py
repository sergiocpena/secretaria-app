"""
Intent classification module.
This file contains the IntentClassifier class for detecting different types of intents in messages.
"""
import logging
import json
import openai
import time as time_module

logger = logging.getLogger(__name__)

class IntentClassifier:
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
                intent_details: any - Additional details about the intent
        """
        # Log the incoming message (truncated for privacy/brevity)
        truncated_message = message[:50] + "..." if len(message) > 50 else message
        logger.info(f"Detecting intent for message: '{truncated_message}'")
        
        # For now, just check for reminder intents
        is_reminder, reminder_intent = self.detect_reminder_intent(message)
        
        if is_reminder:
            logger.info(f"✅ Intent detected: 'reminder' with sub-intent '{reminder_intent}' - will be handled by ReminderAgent")
            return "reminder", reminder_intent
        else:
            logger.info(f"✅ Intent detected: 'general' - will be handled by GeneralAgent")
            return "general", None
    
    def detect_reminder_intent(self, message):
        """
        Detects if a message contains a reminder-related intent using LLM.
        
        Args:
            message (str): The user message to analyze
            
        Returns:
            tuple: (is_reminder, intent)
                is_reminder: bool - Whether the message contains a reminder intent
                intent: str - The specific reminder intent (create, list, cancel, clarify)
        """
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
            # Fall back to simple keyword detection if LLM fails
            return self._detect_reminder_intent_keywords(message)
    
    def _detect_reminder_intent_keywords(self, message):
        """
        Simple keyword-based fallback for reminder intent detection.
        
        Args:
            message (str): The user message to analyze
            
        Returns:
            tuple: (is_reminder, intent)
        """
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