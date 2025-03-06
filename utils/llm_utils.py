"""
LLM utility functions.
This file provides a centralized way to interact with language models.
"""
import os
import logging
import json
from functools import lru_cache

logger = logging.getLogger(__name__)

# Global client instance
_openai_client = None

def initialize_openai(api_key=None):
    """
    Initialize the OpenAI client with the given API key.
    This should be called once at application startup.
    
    Args:
        api_key: API key to use. If None, will use environment variable.
    
    Returns:
        True if initialization was successful, False otherwise.
    """
    global _openai_client
    
    # Get API key from parameter or environment
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        logger.error("No OpenAI API key found")
        return False
    
    # Try to initialize the client
    try:
        # Try the new OpenAI client
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=api_key)
            logger.info("OpenAI client initialized successfully")
            return True
        except ImportError:
            logger.warning("OpenAI package not installed, trying to install...")
            try:
                import subprocess
                import sys
                subprocess.check_call([sys.executable, "-m", "pip", "install", "openai"])
                from openai import OpenAI
                _openai_client = OpenAI(api_key=api_key)
                logger.info("OpenAI client installed and initialized successfully")
                return True
            except Exception as e:
                logger.error(f"Failed to install OpenAI package: {str(e)}")
                return False
    except Exception as e:
        logger.error(f"Error initializing OpenAI client: {str(e)}")
        return False

def get_openai_client():
    """
    Get the initialized OpenAI client.
    
    Returns:
        The OpenAI client or None if not initialized.
    """
    global _openai_client
    
    if _openai_client is None:
        # Try to initialize with environment variable
        initialize_openai()
    
    return _openai_client

def chat_completion(messages, model="gpt-3.5-turbo", temperature=0.7, response_format=None):
    """
    Send a chat completion request to OpenAI.
    
    Args:
        messages: List of message objects (role, content)
        model: Model to use
        temperature: Temperature parameter
        response_format: Optional response format (e.g., {"type": "json_object"})
        
    Returns:
        The response content or None if the request failed
    """
    client = get_openai_client()
    if not client:
        logger.error("Cannot complete request: OpenAI client not available")
        return None
    
    try:
        # Prepare request parameters
        params = {
            "model": model,
            "messages": messages,
            "temperature": temperature
        }
        
        # Add response format if provided
        if response_format:
            params["response_format"] = response_format
        
        # Make the API call
        response = client.chat.completions.create(**params)
        
        # Return the content
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error in chat completion: {str(e)}")
        return None

def parse_json_response(response_text):
    """
    Parse a JSON response from the LLM.
    
    Args:
        response_text: The text response from the LLM
        
    Returns:
        Parsed JSON object or None if parsing failed
    """
    if not response_text:
        return None
        
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {str(e)}")
        return None 