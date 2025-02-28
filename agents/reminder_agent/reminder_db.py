"""
Reminder-specific database operations.
This file contains functions for managing reminders in the database.
"""
import logging
from datetime import datetime, timezone, timedelta
import pytz
from utils.database import supabase

logger = logging.getLogger(__name__)

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
    """Creates a new reminder"""
    try:
        logger.info(f"Creating reminder for user {user_phone}: {title} at {scheduled_time}")
        
        data = {
            'user_phone': user_phone,
            'title': title,
            'scheduled_time': scheduled_time.isoformat(),
            'is_active': True
        }
        
        result = supabase.table('reminders').insert(data).execute()
        
        if result and result.data:
            reminder_id = result.data[0]['id']
            logger.info(f"Created reminder {reminder_id} for user {user_phone}: {title} at {scheduled_time}")
            return reminder_id
        else:
            logger.error("Failed to create reminder: No data returned from database")
            return None
    except Exception as e:
        logger.error(f"Error creating reminder: {str(e)}")
        return None

def cancel_reminder(reminder_id):
    """Cancels a reminder by marking it as inactive"""
    try:
        logger.info(f"Cancelling reminder {reminder_id}")
        
        result = supabase.table('reminders') \
            .update({'is_active': False}) \
            .eq('id', reminder_id) \
            .execute()
        
        if result and result.data:
            logger.info(f"Reminder {reminder_id} cancelled successfully")
            return True
        else:
            logger.error(f"Failed to cancel reminder {reminder_id}: No data returned from database")
            return False
    except Exception as e:
        logger.error(f"Error cancelling reminder: {str(e)}")
        return False

def get_pending_reminders():
    """Gets reminders that are due to be sent"""
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
        
        return pending_reminders
    except Exception as e:
        logger.error(f"Error getting pending reminders: {str(e)}")
        return []

def get_late_reminders(minutes=30):
    """Gets reminders that are late (past due by specified minutes)"""
    try:
        # Get current time in UTC
        now = datetime.now(timezone.utc)
        # Truncate seconds
        now_truncated = now.replace(second=0, microsecond=0)
        
        # Define threshold for late reminders
        late_threshold = now_truncated - timedelta(minutes=minutes)
        
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
        
        return late_reminders
    except Exception as e:
        logger.error(f"Error getting late reminders: {str(e)}")
        return []

def format_reminder_list_by_time(reminders):
    """Formats a list of reminders for display, grouped by time"""
    if not reminders:
        return "Você não tem lembretes ativos no momento."
    
    response = "📋 *Seus lembretes:*\n"
    
    # Sort reminders by scheduled time
    reminders.sort(key=lambda x: x['scheduled_time'])
    
    # Format each reminder
    for i, reminder in enumerate(reminders, 1):
        scheduled_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
        response += f"{i}. *{reminder['title']}* - {format_datetime(scheduled_time)}\n"
    
    # Add instructions for cancelling
    response += "Para cancelar um lembrete, envie 'cancelar lembrete 2' (usando o número) ou 'cancelar lembrete [título]' (usando o nome)"
    
    return response

def format_created_reminders(reminders):
    """Formats a confirmation message for created reminders"""
    if not reminders:
        return "Nenhum lembrete foi criado."
    
    if len(reminders) == 1:
        reminder = reminders[0]
        return f"✅ Lembrete criado: *{reminder['title']}* para {format_datetime(reminder['time'])}"
    else:
        response = f"✅ {len(reminders)} lembretes criados:\n"
        for i, reminder in enumerate(reminders, 1):
            response += f"{i}. *{reminder['title']}* para {format_datetime(reminder['time'])}\n"
        return response
