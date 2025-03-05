# WhatsApp Assistant

## Architecture

The application is structured with the following components:

- **WhatsApp Agent**: Main entry point that handles incoming messages and routes them to appropriate agents
- **Intent Classifier**: Detects the intent of user messages (e.g., reminders, general conversation)
- **Reminder Agent**: Handles reminder-related functionality (creating, listing, canceling reminders)
- **General Agent**: Handles general conversation