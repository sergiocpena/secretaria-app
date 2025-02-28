"""
Conversation history and general DB operations.
This file contains functions for managing conversation history.
"""
import logging
from utils.database import supabase

logger = logging.getLogger(__name__)

def store_conversation(user_phone, message_content, message_type, is_from_user, agent="DEFAULT"):
    """Store a message in the Supabase conversations table"""
    try:
        data = {
            'user_phone': user_phone,
            'message_content': message_content,
            'message_type': message_type,
            'is_from_user': is_from_user,
            'agent': agent
        }
        
        result = supabase.table('conversations').insert(data).execute()
        logger.info(f"Message stored in database: {message_type} from {'user' if is_from_user else 'agent'}")
        return True
    except Exception as e:
        logger.error(f"Error storing message in database: {str(e)}")
        return False

def get_conversation_history(user_phone, limit=10):
    """Get recent conversation history for a user"""
    try:
        result = supabase.table('conversations') \
            .select('*') \
            .eq('user_phone', user_phone) \
            .order('created_at', desc=True) \
            .limit(limit) \
            .execute()
        
        # Reverse to get chronological order
        conversations = list(reversed(result.data))
        return conversations
    except Exception as e:
        logger.error(f"Error retrieving conversation history: {str(e)}")
        return []
