from django.urls import path
from . import views

urlpatterns = [
    path('', views.chat_ui, name='chat_ui'),
    path('api/chat/', views.chatbot_view, name='chatbot_api'),
]
