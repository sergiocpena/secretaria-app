"""
Media processing utility functions.
This file provides functions for processing different types of media using AI.
"""
import logging
import os
import base64
from utils.whatsapp_utils import download_media
from utils.llm_utils import chat_completion, get_openai_client

logger = logging.getLogger(__name__)

def process_image(image_url):
    """Process an image using OpenAI's vision model"""
    try:
        logger.info(f"Processing image: {image_url}")
        
        # Download the image
        image_data = download_media(image_url)
        if not image_data:
            raise Exception("Failed to download image")
        
        # Convert to base64 for OpenAI API
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        # Use the centralized chat_completion function
        response = chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": """Analyze the provided image in conjunction with the user's prompt, if available. Provide a thorough analysis of the image alone if no prompt is given.

- When a prompt accompanies the image, tailor your analysis to address the user's specific request.
- If the user's prompt is empty, offer a comprehensive analysis based solely on the image.

# Steps

1. **Receive Input:**
   - Identify if both an image and a prompt are provided or if only the image is available.

2. **Analyze Input:**
   - If a prompt is provided, interpret the user's request and focus your analysis on fulfilling it.
   - If no prompt is provided, perform an in-depth analysis of the image.

3. **Synthesize Response:**
   - For image-only inputs, construct a comprehensive and insightful analysis that covers key elements such as objects, context, emotions, and any significant details.
   - For image with prompt inputs, ensure your response directly addresses the user's request.

# Output Format

- Response should be in paragraph form.
- Detailed analysis should focus on readability and depth, ensuring a complete understanding of the image's contents.
- Tailored responses to specific requests should be concise and relevant to the query.

# Examples

### Example 1:
**Input:** 
- Image: [Image of a sunset over the ocean]
- Prompt: "Describe the mood."

**Response:**
- The image depicts a serene and tranquil mood. The warm hues of the sunset blending into the cool tones of the ocean create a relaxing atmosphere. The gentle waves and the fading light evoke a sense of peace and harmony, characteristic of a calm evening by the sea.

### Example 2:
**Input:**
- Image: [Image of a crowded city street]
- Prompt: [Empty]

**Response:**
- The image captures the bustling energy of a crowded city street. People in various attires move briskly along the sidewalks, each seemingly engrossed in their daily activities. The tall buildings, illuminated shop signs, and vibrant billboards add to the urban landscape's dynamic aura. This scene embodies the vibrant and fast-paced life typical of city centers.

# Notes

- Be creative and observant when interpreting images, considering emotional, cultural, and contextual elements.
- Ensure clarity and engagement in your analysis, making it accessible to a broad audience."""
                },
                {
                    "role": "user", 
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image_url", 
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            model="gpt-4o",
            temperature=0.7
        )
        
        return response
    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        return "Desculpe, não consegui analisar esta imagem."

def transcribe_audio(audio_url):
    """Transcribe an audio file using OpenAI's Whisper API"""
    try:
        logger.info(f"Transcribing audio: {audio_url}")
        
        # Download the audio file
        audio_data = download_media(audio_url)
        if not audio_data:
            raise Exception("Failed to download audio")
        
        # Save to a temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_file:
            temp_file.write(audio_data)
            temp_file_path = temp_file.name
        
        # Use the OpenAI client from our utilities
        client = get_openai_client()
        
        if not client:
            raise Exception("OpenAI client not available")
        
        # Open the file and transcribe
        with open(temp_file_path, 'rb') as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="pt"
            )
        
        # Clean up the temporary file
        os.unlink(temp_file_path)
        
        return transcription.text
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}")
        return "Desculpe, não consegui transcrever este áudio." 