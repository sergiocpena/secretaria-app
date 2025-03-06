"""
General agent implementation.
This file contains functions for handling general conversations.
"""
import logging
import os
from datetime import datetime
import pytz

from agents.general_agent.general_db import store_conversation, get_conversation_history
from utils.llm_utils import chat_completion

logger = logging.getLogger(__name__)

def get_ai_response(user_message, conversation_history=None, system_prompt=None):
    """Get a response from the AI model"""
    # Build the messages array
    messages = []
    
    # Add system prompt if provided
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    
    # Add conversation history if provided
    if conversation_history:
        messages.extend(conversation_history)
    
    # Add the user's message
    messages.append({"role": "user", "content": user_message})
    
    # Use the centralized chat_completion function
    response = chat_completion(
        messages=messages,
        model="gpt-4o-mini",  # or whatever model you're using
        temperature=0.7
    )
    
    return response

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
