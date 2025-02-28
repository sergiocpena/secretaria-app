# Database utilities (Supabase)
import os
import logging
from supabase import create_client
from datetime import datetime, timezone, timedelta
import pytz

logger = logging.getLogger(__name__)

# Initialize Supabase client
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')  # Use service role key to bypass RLS
supabase = create_client(supabase_url, supabase_key)

# Define Brazil timezone
BRAZIL_TIMEZONE = pytz.timezone('America/Sao_Paulo')

def to_local_timezone(utc_dt):
    """Converts a UTC datetime to local timezone (Brazil)"""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(BRAZIL_TIMEZONE)

def to_utc_timezone(local_dt):
    """Converts a local datetime to UTC"""
    if local_dt.tzinfo is None:
        # Assume it's local time
        local_dt = BRAZIL_TIMEZONE.localize(local_dt)
    return local_dt.astimezone(timezone.utc)

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

def list_reminders(user_phone):
    """Lists active reminders for a user"""
    try:
        logger.info(f"Listing active reminders for user {user_phone}")
        result = supabase.table('reminders') \
            .select('*') \
            .eq('user_phone', user_phone) \
            .eq('is_active', True) \
            .order('scheduled_time') \
            .execute()
        
        logger.info(f"Found {len(result.data)} active reminders")
        return result.data
    except Exception as e:
        logger.error(f"Error listing reminders: {str(e)}")
        return []

def create_reminder(user_phone, title, scheduled_time):
    """Creates a new reminder in the database"""
    try:
        logger.info(f"Creating reminder for user {user_phone}: {title} at {scheduled_time}")
        data = {
            'user_phone': user_phone,
            'title': title,
            'scheduled_time': scheduled_time.isoformat(),
            'is_active': True
        }
        
        result = supabase.table('reminders').insert(data).execute()
        
        if result.data and len(result.data) > 0:
            reminder_id = result.data[0]['id']
            logger.info(f"Created reminder {reminder_id} for user {user_phone}: {title} at {scheduled_time}")
            return reminder_id
        else:
            logger.error("Failed to create reminder: No data returned")
            return None
    except Exception as e:
        logger.error(f"Error creating reminder: {str(e)}")
        return None

def cancel_reminder(reminder_id):
    """Cancels a reminder by setting is_active to False"""
    try:
        logger.info(f"Cancelling reminder {reminder_id}")
        update_result = supabase.table('reminders') \
            .update({'is_active': False}) \
            .eq('id', reminder_id) \
            .execute()
        
        return True
    except Exception as e:
        logger.error(f"Error cancelling reminder {reminder_id}: {str(e)}")
        return False

def get_pending_reminders():
    """Gets all pending reminders that should be sent now"""
    try:
        # Get current time in UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds for comparison
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Get all active reminders
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .execute()
        
        reminders = result.data
        logger.info(f"Found {len(reminders)} active reminders")
        
        # Filter reminders manually to ignore seconds
        pending_reminders = []
        for reminder in reminders:
            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
            # Truncate seconds for comparison
            scheduled_time_truncated = scheduled_time.replace(second=0, microsecond=0)
            
            # Only include reminders that are due (current time >= scheduled time)
            if now_truncated >= scheduled_time_truncated:
                pending_reminders.append(reminder)
        
        logger.info(f"Found {len(pending_reminders)} pending reminders after time comparison")
        return pending_reminders
    except Exception as e:
        logger.error(f"Error getting pending reminders: {str(e)}")
        return []

def get_late_reminders(minutes_threshold=30):
    """Gets reminders that are late by the specified threshold"""
    try:
        # Get current time in UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Define late threshold
        late_threshold = now_truncated - timedelta(minutes=minutes_threshold)
        
        # Get all active reminders
        result = supabase.table('reminders') \
            .select('*') \
            .eq('is_active', True) \
            .execute()
        
        reminders = result.data
        
        # Filter late reminders manually
        late_reminders = []
        for reminder in reminders:
            scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
            # Truncate seconds
            scheduled_time_truncated = scheduled_time.replace(second=0, microsecond=0)
            if scheduled_time_truncated <= late_threshold:
                late_reminders.append(reminder)
        
        if late_reminders:
            logger.info(f"Found {len(late_reminders)} late reminders")
        
        return late_reminders
    except Exception as e:
        logger.error(f"Error getting late reminders: {str(e)}")
        return []

def format_reminder_list_by_time(reminders, include_cancel_instructions=True):
    """Formats a list of reminders for display, sorted by time proximity"""
    if not reminders:
        return "Você não tem lembretes ativos no momento."
    
    # Sort reminders by scheduled time
    sorted_reminders = sorted(reminders, key=lambda r: datetime.fromisoformat(r['scheduled_time'].replace('Z', '+00:00')))
    
    response = "📋 *Seus lembretes:*\n"
    for i, reminder in enumerate(sorted_reminders, 1):
        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
        formatted_time = format_datetime(scheduled_time)
        response += f"{i}. *{reminder['title']}* - {formatted_time}\n"
    
    if include_cancel_instructions:
        response += "\nPara cancelar um lembrete, envie 'cancelar lembrete 2' (usando o número) ou 'cancelar lembrete [título]' (usando o nome)"
    
    return response

def format_datetime(dt):
    """Formats a datetime for user-friendly display in Portuguese"""
    # Convert to local timezone
    local_dt = to_local_timezone(dt)
    
    # Get current date in local timezone
    now = datetime.now(BRAZIL_TIMEZONE)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    # Format the date part
    if local_dt.date() == today:
        date_str = "hoje"
    elif local_dt.date() == tomorrow:
        date_str = "amanhã"
    else:
        # Format the date in Portuguese
        date_str = local_dt.strftime("%d/%m/%Y")
    
    # Format the time
    time_str = local_dt.strftime("%H:%M")
    
    return f"{date_str} às {time_str}"

def format_created_reminders(created_reminders):
    """Formats a response for newly created reminders"""
    if not created_reminders:
        return "❌ Não consegui criar os lembretes. Por favor, tente novamente."
    
    # Sort reminders by scheduled time
    sorted_reminders = sorted(created_reminders, key=lambda r: r['time'])
    
    if len(sorted_reminders) == 1:
        reminder = sorted_reminders[0]
        return f"✅ Lembrete criado: *{reminder['title']}* para {format_datetime(reminder['time'])}"
    else:
        response = f"✅ {len(sorted_reminders)} lembretes criados:\n\n"
        for i, reminder in enumerate(sorted_reminders, 1):
            response += f"{i}. *{reminder['title']}* - {format_datetime(reminder['time'])}\n"
        return response 