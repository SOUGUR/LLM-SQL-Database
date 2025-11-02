import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views import View
from .services.rag_query import process_text_to_sql

@method_decorator(csrf_exempt, name='dispatch')
class TextToSQLView(View):
    async def post(self, request):
        try:
            body = json.loads(request.body)
            question = body.get("question", "").strip()
            if not question:
                return JsonResponse({"error": "Question is required"}, status=400)
            
            result = await process_text_to_sql(question)
            return JsonResponse(result)
            
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)