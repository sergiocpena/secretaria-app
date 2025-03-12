"""
Media utilities for processing images and audio files.
"""
import os
import logging
import requests
import tempfile
import base64
import openai
from io import BytesIO

logger = logging.getLogger(__name__)

def process_image(image_url):
    """
    Process an image using OpenAI's vision model.
    
    Args:
        image_url: URL of the image to process
        
    Returns:
        str: Description of the image
    """
    try:
        logger.info(f"Processing image from URL: {image_url}")
        
        # Download the image
        response = requests.get(image_url)
        if response.status_code != 200:
            logger.error(f"Failed to download image: {response.status_code}")
            return "Não consegui baixar a imagem."
        
        # Convert to base64
        image_data = base64.b64encode(response.content).decode('utf-8')
        
        # Use OpenAI API directly
        response = openai.ChatCompletion.create(
            model="gpt-4o",  # Use a model with vision capabilities
            messages=[
                {
                    "role": "system",
                    "content": "Você é um assistente que descreve imagens em português do Brasil. Seja detalhado mas conciso."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Descreva esta imagem em detalhes."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        
        description = response.choices[0].message.content
        logger.info(f"Generated image description: {description[:100]}... (truncated)")
        
        return description
        
    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        return "Ocorreu um erro ao processar a imagem."

def transcribe_audio(audio_url):
    """
    Transcribe audio using OpenAI's Whisper model.
    
    Args:
        audio_url: URL of the audio file to transcribe
        
    Returns:
        str: Transcription of the audio
    """
    try:
        logger.info(f"Transcribing audio from URL: {audio_url}")
        
        # Download the audio file
        response = requests.get(audio_url)
        if response.status_code != 200:
            logger.error(f"Failed to download audio: {response.status_code}")
            return "Não consegui baixar o áudio."
        
        # Save to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name
        
        try:
            # Use OpenAI API directly
            with open(temp_path, "rb") as audio_file:
                transcript = openai.Audio.transcribe(
                    model="whisper-1",
                    file=audio_file,
                    language="pt"
                )
            
            transcription = transcript.text
            logger.info(f"Generated transcription: {transcription[:100]}... (truncated)")
            
            return transcription
            
        finally:
            # Clean up the temporary file
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}")
        return "Ocorreu um erro ao transcrever o áudio." 