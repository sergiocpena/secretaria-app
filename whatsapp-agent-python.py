from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta, time
import os
import requests
import threading
import time as time_module
import logging
import pytz
import openai
from dotenv import load_dotenv

# Import from our modules
from utils.database import supabase
from agents.general_agent.general_db import store_conversation, get_conversation_history
from agents.reminder_agent.reminder_db import (
    list_reminders, create_reminder, cancel_reminder, 
    get_pending_reminders, get_late_reminders,
    format_reminder_list_by_time, format_created_reminders,
    to_local_timezone, to_utc_timezone, format_datetime,
    BRAZIL_TIMEZONE
)
from agents.general_agent.general_agent import get_ai_response, handle_message, get_conversation_context
from agents.reminder_agent.reminder_agent import ReminderAgent
from intent_classifier.classifier import IntentClassifier
from utils.whatsapp_utils import (
    parse_twilio_request, send_whatsapp_message, start_message_sender,
    webhook_handler, send_direct_message_handler, process_message_async
)
from utils.media_utils import process_image, transcribe_audio

# Configure logging
log_level = os.getenv('LOG_LEVEL', 'INFO')
health_log_level = os.getenv('HEALTH_LOG_LEVEL', 'DEBUG')

logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure health check logger
health_logger = logging.getLogger('health_checks')
health_logger.setLevel(getattr(logging, health_log_level))

# Add this after your logging configuration
class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        # Filter out health check logs
        return 'Health check at' not in record.getMessage()

# Apply the filter to the root logger
logging.getLogger().addFilter(HealthCheckFilter())

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Initialize OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Initialize the classifier
intent_classifier = IntentClassifier()

# Initialize the ReminderAgent with the OpenAI API key
reminder_agent = ReminderAgent(api_key=os.getenv('OPENAI_API_KEY'))

# Use a lazy initialization pattern:
_twilio_client = None

def get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
    return _twilio_client

# ===== EXISTING FUNCTIONS =====

def ping_self():
    app_url = os.getenv('APP_URL', 'https://secretaria-app.onrender.com')
    
    while True:
        try:
            requests.get(app_url, timeout=5)
            logger.info(f"Self-ping successful")
        except Exception as e:
            logger.error(f"Self-ping failed: {str(e)}")
        
        # Sleep for 10 minutes (600 seconds)
        time_module.sleep(600)

# Start the self-ping in a background thread
def start_self_ping():
    ping_thread = threading.Thread(target=ping_self, daemon=True)
    ping_thread.start()
    logger.info("Self-ping background thread started")

# Define a custom process_message function that passes all required dependencies
def process_message_wrapper(from_number, body, num_media, form_values):
    process_message_async(
        from_number, body, num_media, form_values,
        intent_classifier, reminder_agent, handle_message, 
        get_ai_response, process_image, transcribe_audio
    )

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Twilio WhatsApp messages"""
    return webhook_handler(request, process_message_wrapper)

@app.route('/send_message', methods=['POST'])
def send_direct_message():
    """Endpoint to send messages outside of the webhook context"""
    return send_direct_message_handler(request)

# Add this to your startup code
try:
    logger.info("Checking Twilio credentials...")
    account = get_twilio_client().api.accounts(os.getenv('TWILIO_ACCOUNT_SID')).fetch()
    logger.info(f"Twilio account status: {account.status}")
    
    # Try to list messages to verify API access
    messages = get_twilio_client().messages.list(limit=1)
    logger.info(f"Successfully retrieved {len(messages)} messages from Twilio")
except Exception as e:
    logger.error(f"Error checking Twilio credentials: {str(e)}")

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    # Use the health logger instead of the main logger
    health_logger.debug(f"Health check at {datetime.now().isoformat()}")
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/api/check-reminders', methods=['POST'])
def api_check_reminders():
    """Endpoint para verificar e enviar lembretes pendentes"""
    try:
        # Verificar autenticação
        api_key = request.headers.get('X-API-Key')
        if api_key != os.getenv('REMINDER_API_KEY'):
            return jsonify({"error": "Unauthorized"}), 401
        
        # Ensure message sender thread is running
        if not hasattr(app, 'message_sender_thread') or not app.message_sender_thread.is_alive():
            logger.info("Starting message sender thread for reminder check")
            app.message_sender_thread = start_message_sender()
        
        # Processar lembretes using the reminder agent
        result = reminder_agent.check_and_send_reminders()
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error in check-reminders endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Start the message sender thread
    app.message_sender_thread = start_message_sender()
    
    # Start the reminder checker thread
    reminder_checker_thread = reminder_agent.start_reminder_checker()
    
    # Start the self-ping thread if needed
    if os.getenv('ENABLE_SELF_PING', 'false').lower() == 'true':
        start_self_ping()
    
    # Start the Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)