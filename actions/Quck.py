import streamlit as st
import requests

st.write("Quick questions:")
col1, col2, col3 = st.columns(3)
user_input = None

with col1:
    if st.button("🌍 Suggest a destination"):
        user_input = "where should I travel this summer?"
with col2:
    if st.button("☀️ Check the weather"):
        user_input = "what's the weather in my destination?"
with col3:
    if st.button("💰 Budget tips"):
        user_input = "how can I travel on a budget?"

# fall through to chat_input if no button was clicked
typed = st.chat_input("Or type your own message...")
if typed:
    user_input = typed

if user_input:
    try:
        response = requests.post(
            "http://localhost:5005/webhooks/rest/webhook",
            json={"sender": "quick-questions", "message": user_input},
            timeout=30,
        )
        response.raise_for_status()
        for message in response.json():
            if message.get("text"):
                st.write(message["text"])
    except requests.RequestException as exc:
        st.error(f"Couldn't reach the travel advisor: {exc}")
