import streamlit as st
import pandas as pd
import random
import datetime
import os
import glob

from docx import Document
from docx.shared import Inches

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import firebase_admin
from firebase_admin import credentials, firestore

# Set wide layout
st.set_page_config(layout="wide")

# Initialize Firebase
firebase_creds = st.secrets["firebase_service_account"].to_dict()
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_creds)
    firebase_admin.initialize_app(cred)
db = firestore.client()

### Session State Setup
def initialize_state():
    keys = [
        "authenticated", "user_name", "assigned_passcode", "recipient_email",
        "df", "answered", "selected_answer", "question_row", "review_sent"
    ]
    for key in keys:
        if key not in st.session_state:
            st.session_state[key] = False if key == "authenticated" else ""

### Helper Functions
def check_and_add_passcode(passcode):
    passcode_str = str(passcode)
    if passcode_str.lower() == "password":
        return False
    doc_ref = db.collection("shelf_records_freetext").document(passcode_str)
    if not doc_ref.get().exists:
        doc_ref.set({"processed": True})
        return False
    else:
        return True

def generate_review_doc(row, user_answer, output_filename="review.docx"):
    def safe_text(val):
        return str(val) if pd.notna(val) else ""

    doc = Document()
    doc.add_heading("Review of Incorrect Question", level=1)
    doc.add_heading(f"Student: {st.session_state.user_name}", level=2)
    doc.add_heading(f"Question ({row['record_id']}):", level=2)
    doc.add_paragraph(safe_text(row["anchor"]))

    sections = [
        ("Chief Complaint", row.get("cc", "")),
        ("History of Present Illness", row.get("hpi", "")),
        ("Past Medical History", row.get("pmhx", "")),
        ("Medications", row.get("meds", "")),
        ("Allergies", row.get("allergies", "")),
        ("Immunizations", row.get("immunizations", "")),
        ("Social History", row.get("shx", "")),
        ("Family History", row.get("fhx", "")),
        ("Vital Signs", row.get("vs", "")),
        ("Physical Exam", row.get("pe", ""))
    ]

    for title, content in sections:
        doc.add_heading(title, level=2)
        doc.add_paragraph(safe_text(content))

    doc.add_heading("Student Answer:", level=2)
    doc.add_paragraph(safe_text(user_answer))

    doc.add_heading("Correct Answer:", level=2)
    doc.add_paragraph(safe_text(row["answer"]))

    doc.add_heading("Explanation:", level=2)
    doc.add_paragraph(safe_text(row["answer_explanation"]))

    doc.save(output_filename)
    return output_filename


def send_email_with_attachment(to_emails, subject, body, attachment_path):
    from_email = st.secrets["general"]["email"]
    password = st.secrets["general"]["email_password"]
    
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = ', '.join(to_emails)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    
    with open(attachment_path, 'rb') as attachment:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(attachment_path))
        msg.attach(part)
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(from_email, password)
            server.send_message(msg, from_addr=from_email, to_addrs=to_emails)
        st.success("Email sent successfully!")
    except Exception as e:
        st.error(f"Error sending email: {e}")

### Login Screen
def login_screen():
    st.title("Shelf Examination Login")
    passcode_input = st.text_input("Enter your assigned passcode", type="password")
    user_name = st.text_input("Enter your name")

    if st.button("Login"):
        if "recipients" not in st.secrets:
            st.error("Recipient emails not configured.")
            return
        if passcode_input not in st.secrets["recipients"]:
            st.error("Invalid passcode.")
            return
        if not user_name:
            st.error("Please enter your name.")
            return

        st.session_state.authenticated = True
        st.session_state.assigned_passcode = passcode_input
        st.session_state.user_name = user_name
        st.session_state.recipient_email = st.secrets["recipients"][passcode_input]
        st.rerun()

def exam_screen_freetext():
    st.title("Shelf Examination – Free Text Format")
    st.write(f"Welcome, **{st.session_state.user_name}**!")

    if not st.session_state.question_row:
        df = pd.read_csv("clinical_case_final_format.csv")
        selected = df.sample(1).iloc[0].to_dict()
        st.session_state.question_row = selected
        st.session_state.answered = False
        st.session_state.selected_answer = ""
        st.session_state.review_sent = False

    row = st.session_state.question_row

    with st.sidebar:
        st.header("Clinical Information")
        for label in ["cc", "hpi", "pmhx", "meds", "allergies", "immunizations", "shx", "fhx", "vs", "pe"]:
            with st.expander(label.upper()):
                st.write(row.get(label, ""))

    st.subheader("Clinical Question")
    st.write(row.get("anchor", ""))

    all_choices = [c.strip() for c in str(row.get("choices", "")).split(",")]
    user_input = st.text_input("Type your answer here:", value=st.session_state.selected_answer, key="freetext_input")

    if not st.session_state.answered and user_input:
        matches = [c for c in all_choices if user_input.lower() in c.lower()]
        st.write("Matching options:")
        for match in matches[:5]:  # Limit to 5 matches
            if st.button(match, key=f"choice_btn_{match}"):
                st.session_state.selected_answer = match
                st.session_state.answered = True
                correct_answer = row["answer"].strip().lower()
                selected = match.strip().lower()

                if selected == correct_answer:
                    st.success("Correct!")
                else:
                    st.error("Incorrect.")
                    st.write(f"**Correct Answer:** {row['answer']}")
                    st.info(row.get("answer_explanation", ""))

                    # Check if this passcode has already been used
                    locked = check_and_add_passcode(st.session_state.assigned_passcode)
                    if not locked and not st.session_state.review_sent:
                        filename = f"review_{st.session_state.user_name}_{row['record_id']}.docx"
                        generate_review_doc(row, match, filename)
                        send_email_with_attachment(
                            to_emails=[st.session_state.recipient_email],
                            subject="Review of Incorrect Answer",
                            body="Please find attached a review of the incorrect response.",
                            attachment_path=filename
                        )
                        st.session_state.review_sent = True
                    else:
                        st.info("This passcode has already been used. No review email will be sent.")

    if st.session_state.answered:
        if st.button("Try Another Case"):
            st.session_state.question_row = ""
            st.session_state.answered = False
            st.session_state.selected_answer = ""
            st.session_state.review_sent = False
            st.rerun()


### Main App Logic
def main():
    initialize_state()
    if not st.session_state.authenticated:
        login_screen()
    else:
        exam_screen_freetext()

if __name__ == "__main__":
    main()

