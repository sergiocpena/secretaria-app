"""
Core database functionality (just Supabase init).
This file provides the basic Supabase client for use by other modules.
"""
import os
import logging
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import pytz

# Import from our datetime utils module
from utils.datetime_utils import to_local_timezone, to_utc_timezone, format_datetime, BRAZIL_TIMEZONE

# Load environment variables if not already loaded
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Initialize Supabase client
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')  # Make sure this is the service role key, not the anon key

# Create and export the client
supabase = create_client(supabase_url, supabase_key)

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

def execute_query(table, query_type, data=None, filters=None):
    """Generic function to execute Supabase queries"""
    try:
        if query_type == 'select':
            query = supabase.table(table).select('*')
            if filters:
                for key, value in filters.items():
                    query = query.eq(key, value)
            return query.execute()
        elif query_type == 'insert':
            return supabase.table(table).insert(data).execute()
        elif query_type == 'update':
            query = supabase.table(table).update(data)
            if filters:
                for key, value in filters.items():
                    query = query.eq(key, value)
            return query.execute()
        elif query_type == 'delete':
            query = supabase.table(table)
            if filters:
                for key, value in filters.items():
                    query = query.eq(key, value)
            return query.delete().execute()
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return None 