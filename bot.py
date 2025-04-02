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
    print("Service account key file not found! Please mount /creds with service-account-key.json")
    exit(1)

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

@bot.event
async def on_ready():
    print(f'Bot is ready as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user or not message.attachments:
        return
    if message.channel.name != 'work-calendar':  # Restrict to specific channel
        return

    for attachment in message.attachments:
        if attachment.filename.endswith(('.png', '.jpg', '.jpeg')):
            # Download image
            image_data = await attachment.read()
            image = Image.open(io.BytesIO(image_data))
            
            # OCR
            text = pytesseract.image_to_string(image)
            
            # Current date for reference
            current_date = datetime.now()
            two_months_later = current_date + timedelta(days=60)
            
            # Parse events
            events = []
            lines = text.split('\n')
            current_day = None
            current_date = None
            current_month = current_date.month

            for line in lines:
                line = line.strip()
                # Match day and date (e.g., "Mon 14")
                day_match = re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})$', line)
                if day_match:
                    current_day, day_num = day_match.groups()
                    day_num = int(day_num)
                    
                    # Find the correct month and year for this date
                    for month_offset in range(0, 3):  # Check current month and next two months
                        test_date = current_date.replace(day=1, month=current_date.month + month_offset)
                        if test_date.month > 12:
                            test_date = test_date.replace(year=test_date.year + 1, month=1)
                        try:
                            test_date = test_date.replace(day=day_num)
                            # Check if the day of the week matches
                            if test_date.strftime('%a')[:3] == current_day and current_date <= test_date <= two_months_later:
                                current_date = test_date
                                current_month = current_date.month
                                break
                        except ValueError:
                            continue
                    continue
                
                # Match shift time and event (e.g., "10:00 AM - 7:00 PM [8:00]")
                shift_match = re.match(r'(\d{1,2}:\d{2}\s+[AP]M)\s*-\s*(\d{1,2}:\d{2}\s+[AP]M)\s*\[\d{1,2}:\d{2}\]', line)
                if shift_match and current_date:
                    start_time, end_time = shift_match.groups()
                    
                    # Get the event title (next line after the shift time)
                    event_title = lines[lines.index(line) + 1].strip()
                    if not event_title or 'Associate' not in event_title:
                        event_title = lines[lines.index(line) + 2].strip()
                    
                    # Parse start and end times
                    start_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %I:%M %p')
                    end_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {end_time}", '%Y-%m-%d %I:%M %p')
                    
                    # Create event
                    event = {
                        'summary': event_title,
                        'start': {
                            'dateTime': start_dt.isoformat(),
                            'timeZone': 'America/Phoenix'  # Set via TZ env variable
                        },
                        'end': {
                            'dateTime': end_dt.isoformat(),
                            'timeZone': 'America/Phoenix'
                        }
                    }
                    events.append(event)
            
            # Add events to Google Calendar
            for event in events:
                service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
            
            await message.channel.send(f'Added {len(events)} events to your Google Calendar!')

bot.run(DISCORD_BOT_TOKEN)