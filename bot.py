import discord
from discord.ext import commands
import pytesseract
from PIL import Image
import io
from googleapiclient.discovery import build
from google.oauth2 import service_account
import re
from datetime import datetime, timedelta
import os
import logging
import traceback

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Environment variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID', 'primary')  # Default to 'primary' if not set

# Google Calendar setup with Service Account
creds_path = '/creds/service-account-key.json'
if not os.path.exists(creds_path):
    logger.error("Service account key file not found! Please mount /creds with service-account-key.json")
    exit(1)

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

@bot.event
async def on_ready():
    logger.info(f'Bot is ready as {bot.user}')
    # Send a startup message to the #work-calendar channel
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name='work-calendar')
        if channel:
            await channel.send("Bot is now active and ready to process screenshots!")
            break
    else:
        logger.warning("Could not find #work-calendar channel to send startup message.")

@bot.event
async def on_message(message):
    logger.info(f"Received message from {message.author} in channel {message.channel.name}")
    if message.author == bot.user:
        logger.info("Message is from the bot itself, ignoring.")
        return
    if not message.attachments:
        logger.info("Message has no attachments, ignoring.")
        return
    if message.channel.name != 'work-calendar':
        logger.info(f"Message is not in #work-calendar (channel: {message.channel.name}), ignoring.")
        return

    logger.info(f"Processing message with {len(message.attachments)} attachments")
    for attachment in message.attachments:
        if attachment.filename.endswith(('.png', '.jpg', '.jpeg')):
            logger.info(f"Found image attachment: {attachment.filename}")
            try:
                # Download image
                image_data = await attachment.read()
                image = Image.open(io.BytesIO(image_data))
                
                # OCR
                logger.info("Performing OCR on image")
                try:
                    text = pytesseract.image_to_string(image)
                except Exception as e:
                    logger.error(f"OCR error: {str(e)}")
                    await message.channel.send("Error: Failed to perform OCR on the image. Please try a different image or ensure the text is clear.")
                    return
                logger.info(f"OCR result:\n{text}")
                
                # Current date for reference
                current_date = datetime.now()
                two_months_later = current_date + timedelta(days=60)
                
                # Parse events
                events = []
                lines = text.split('\n')
                current_day = None
                current_date = None  # We'll set this when we parse a date

                for i, line in enumerate(lines):
                    line = line.strip()
                    # Match day and date (e.g., "Mon 31" or "Wed 2")
                    day_match = re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})$', line)
                    if day_match:
                        current_day, day_num = day_match.groups()
                        day_num = int(day_num)
                        
                        # Find the correct month and year for this date
                        for month_offset in range(0, 3):  # Check current month and next two months
                            test_date = datetime.now().replace(day=1, month=datetime.now().month + month_offset)
                            if test_date.month > 12:
                                test_date = test_date.replace(year=test_date.year + 1, month=1)
                            try:
                                test_date = test_date.replace(day=day_num)
                                # Check if the day of the week matches
                                if test_date.strftime('%a')[:3] == current_day and datetime.now() <= test_date <= two_months_later:
                                    current_date = test_date
                                    logger.info(f"Parsed date: {current_date.strftime('%Y-%m-%d')} ({current_day})")
                                    break
                            except ValueError:
                                continue
                        continue
                    
                    # Match shift time (e.g., "10:30 AM - 7:30 PM [8:00]")
                    shift_match = re.match(r'(\d{1,2}:\d{2}\s+[AP]M)\s*-\s*(\d{1,2}:\d{2}\s+[AP]M)\s*\[\d{1,2}:\d{2}\]', line)
                    if shift_match and current_date:
                        start_time, end_time = shift_match.groups()
                        
                        # Get the event title (next 1-2 lines after the shift time)
                        event_title = None
                        for j in range(1, 3):
                            if i + j < len(lines):
                                next_line = lines[i + j].strip()
                                if 'Associate' in next_line:
                                    continue
                                if next_line and 'Store' in next_line:
                                    event_title = next_line
                                    break
                        if not event_title:
                            logger.warning(f"No event title found for shift on {current_date.strftime('%Y-%m-%d')}")
                            continue
                        
                        # Parse start and end times
                        start_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %I:%M %p')
                        end_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {end_time}", '%Y-%m-%d %I:%M %p')
                        logger.info(f"Parsed shift: {start_dt} to {end_dt}, Title: {event_title}")
                        
                        # Check for existing events on this day
                        day_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        day_end = day_start + timedelta(days=1)
                        try:
                            existing_events = service.events().list(
                                calendarId=CALENDAR_ID,
                                timeMin=day_start.isoformat() + 'Z',
                                timeMax=day_end.isoformat() + 'Z',
                                singleEvents=True
                            ).execute().get('items', [])
                        except Exception as e:
                            logger.error(f"Google Calendar API error (list events): {str(e)}")
                            await message.channel.send("Error: Failed to access Google Calendar (list events). Please check the Service Account permissions.")
                            return
                        
                        # Delete existing events for this day
                        for event in existing_events:
                            try:
                                service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
                                logger.info(f"Deleted existing event on {day_start.strftime('%Y-%m-%d')}: {event['summary']}")
                            except Exception as e:
                                logger.error(f"Google Calendar API error (delete event): {str(e)}")
                                await message.channel.send("Error: Failed to delete existing events in Google Calendar. Please check the Service Account permissions.")
                                return
                        
                        # Create new event
                        event = {
                            'summary': event_title,
                            'start': {
                                'dateTime': start_dt.isoformat(),
                                'timeZone': 'America/Phoenix'
                            },
                            'end': {
                                'dateTime': end_dt.isoformat(),
                                'timeZone': 'America/Phoenix'
                            }
                        }
                        events.append(event)
                
                # Add events to Google Calendar
                for event in events:
                    try:
                        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                        logger.info(f"Added event: {event['summary']} on {event['start']['dateTime']}")
                    except Exception as e:
                        logger.error(f"Google Calendar API error (insert event): {str(e)}")
                        await message.channel.send("Error: Failed to add events to Google Calendar. Please check the Service Account permissions.")
                        return
                
                await message.channel.send(f'Added {len(events)} events to your Google Calendar!')
                logger.info(f"Successfully added {len(events)} events to Google Calendar")
            except Exception as e:
                logger.error(f"Unexpected error: {traceback.format_exc()}")
                await message.channel.send(f"Unexpected error occurred: {str(e)}. Please try again or contact the bot owner.")

    await bot.process_commands(message)

if not DISCORD_BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

bot.run(DISCORD_BOT_TOKEN)