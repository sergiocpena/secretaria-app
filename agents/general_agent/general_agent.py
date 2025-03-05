"""
General agent implementation.
This file contains functions for handling general conversations.
"""
import logging
import openai
import os
from datetime import datetime
import pytz

from agents.general_agent.general_db import store_conversation, get_conversation_history

logger = logging.getLogger(__name__)

def get_ai_response(message, is_audio_transcription=False):
    """
    Get a response from the AI for a general conversation message.
    """
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

def handle_message(from_number, message_body, message_type='text'):
    """
    Process an incoming message and generate a response.
    """
    try:
        # Store the incoming message
        store_conversation(from_number, message_body, message_type, True)
        
        # Get AI response for general conversation
        response = get_ai_response(message_body, is_audio_transcription=(message_type == 'audio'))
        
        # Store the response
        store_conversation(from_number, response, 'text', False)
        
        return response
    except Exception as e:
        logger.error(f"Error handling message: {str(e)}")
        return "Desculpe, ocorreu um erro ao processar sua mensagem."

def get_conversation_context(from_number, limit=5):
    """
    Get recent conversation history for context.
    """
    try:
        conversations = get_conversation_history(from_number, limit)
        
        # Format the conversations for the AI
        context = []
        for conv in conversations:
            role = "user" if conv['is_from_user'] else "assistant"
            content = conv['message_content']
            context.append({"role": role, "content": content})
        
        return context
    except Exception as e:
        logger.error(f"Error getting conversation context: {str(e)}")
        return []
