from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
from .utils import ssn_tracker
from django.shortcuts import render

# Load data once (you can refresh periodically with celery or signal if needed)
summary_df = ssn_tracker.load_summary()

@csrf_exempt
def chatbot_view(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        message = data.get('message', '').strip()

        # Check if message is an SSN (with or without dashes)
        ssn_input = message.replace('-', '').strip()
        if ssn_input.isdigit() and len(ssn_input) == 9:
            response = ssn_tracker.get_ssn_history(message)
        else:
            response = "Please enter a valid 9-digit SSN (e.g., 123-45-6789)."

        return JsonResponse({'response': response})
    return JsonResponse({'error': 'Invalid request'}, status=400)

def chat_ui(request):
    return render(request, 'chat/chat_ui.html') 

