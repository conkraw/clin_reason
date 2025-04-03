import streamlit as st
import pandas as pd
import random
import datetime
import os
import glob
import re

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

# Session state initialization
def initialize_state():
    keys = ["authenticated", "user_name", "assigned_passcode", "recipient_email", 
            "question_row", "selected_diagnoses", "search_input", "answered", "review_sent"]
    for key in keys:
        if key not in st.session_state:
            if key in ["authenticated", "answered", "review_sent"]:
                st.session_state[key] = False
            else:
                st.session_state[key] = ""

# Helper function: check passcode (for locking the case)
def check_and_add_passcode(passcode):
    passcode_str = str(passcode)
    if passcode_str.lower() == "password":
        return False
    doc_ref = db.collection("shelf_records_prioritized").document(passcode_str)
    if not doc_ref.get().exists:
        doc_ref.set({"processed": True})
        return False
    else:
        return True

# Helper function: send email with attachment
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

# Helper function: format the physical exam into bullet lines based on colon-separated labels.
def format_physical_exam(pe_text):
    if not isinstance(pe_text, str):
        return []
    pattern = r'([A-Z][a-zA-Z ]+):'
    parts = re.split(pattern, pe_text)
    formatted_lines = []
    for i in range(1, len(parts), 2):
        label = parts[i].strip()
        description = parts[i + 1].strip()
        formatted_lines.append(f"{label}: {description}")
    return formatted_lines

def safe_text(val):
    return str(val) if pd.notna(val) else ""

# Generate a DOCX review document for a prioritized answer.
def generate_review_doc_prioritized(row, user_order, output_filename="review.docx"):
    doc = Document()
    doc.add_heading("Review of Incorrect Prioritized Diagnosis", level=1)
    doc.add_heading(f"Student: {st.session_state.user_name}", level=2)
    doc.add_heading(f"Case ({row['record_id']}):", level=2)
    doc.add_paragraph(safe_text(row["anchor"]))
    sections = {
         "Chief Complaint": row.get("cc", ""),
         "History of Present Illness": row.get("hpi", ""),
         "Past Medical History": row.get("pmhx", ""),
         "Medications": row.get("meds", ""),
         "Allergies": row.get("allergies", ""),
         "Immunizations": row.get("immunizations", ""),
         "Social History": row.get("shx", ""),
         "Family History": row.get("fhx", ""),
         "Vital Signs": row.get("vs", ""),
         "Physical Exam": row.get("pe", "")
    }
    for title, content in sections.items():
         if pd.notna(content) and str(content).strip():
              doc.add_heading(title, level=2)
              if title == "Physical Exam":
                  lines = format_physical_exam(content)
                  for line in lines:
                      if ":" in line:
                          label, text = line.split(":", 1)
                          p = doc.add_paragraph()
                          run1 = p.add_run(f"{label.strip()}: ")
                          run1.bold = True
                          p.add_run(text.strip())
                      else:
                          doc.add_paragraph(line)
              else:
                  doc.add_paragraph(safe_text(content))
    # Add student's prioritized answer:
    doc.add_heading("Your Prioritized Diagnosis:", level=2)
    for i, diag in enumerate(user_order):
         doc.add_paragraph(f"{i+1}. {diag}")
    # Add correct prioritized answer:
    correct_order = [safe_text(row.get("answer", "")).strip(), 
                     safe_text(row.get("sec_dx", "")).strip(), 
                     safe_text(row.get("thir_dx", "")).strip()]
    doc.add_heading("Correct Prioritized Diagnosis:", level=2)
    for i, diag in enumerate(correct_order):
         doc.add_paragraph(f"{i+1}. {diag}")
    # Explanation:
    doc.add_heading("Explanation:", level=2)
    doc.add_paragraph(safe_text(row.get("answer_explanation", "")))
    doc.save(output_filename)
    return output_filename

# Login screen
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

def exam_screen_prioritized():
    st.title("Shelf Examination – Prioritized Differential Diagnosis")
    st.write(f"Welcome, {st.session_state.user_name}!")

    # 1) LOAD A RANDOM CASE IF NOT ALREADY LOADED
    if not st.session_state.question_row:
        # Load all CSV files with 'prioritized' in their filename
        csv_files = glob.glob("*.csv")
        csv_files = [file for file in csv_files if "prioritized" in file.lower()]
        df_list = [pd.read_csv(file) for file in csv_files]
        df = pd.concat(df_list, ignore_index=True)

        selected = df.sample(1).iloc[0].to_dict()
        st.session_state.question_row = selected
        st.session_state.selected_diagnoses = []
        st.session_state.search_input = ""
        st.session_state.answered = False
        st.session_state.review_sent = False

    row = st.session_state.question_row

    # 2) SIDEBAR: Show each section in an expander
    with st.sidebar:
        st.header("Clinical Information")
        label_map = {
            "cc": "Chief Complaint",
            "hpi": "History of Present Illness",
            "pmhx": "Past Medical History",
            "meds": "Medications",
            "allergies": "Allergies",
            "immunizations": "Immunizations",
            "shx": "Social History",
            "fhx": "Family History",
            "vs": "Vital Signs",
            "pe": "Physical Exam",
        }
        for key, display_label in label_map.items():
            content = row.get(key, "")
            if pd.notna(content) and str(content).strip():
                with st.expander(display_label, expanded=False):
                    if key == "pe":
                        # Format Physical Exam into bullet lines
                        lines = format_physical_exam(content)
                        for line in lines:
                            st.markdown(f"- {line}")
                    else:
                        st.write(content)

    # 3) MAIN PROMPT
    st.subheader(row.get("anchor", "Please select and prioritize 3 diagnoses:"))
    st.write(
        "Type to search for a diagnosis, then click to add it to your prioritized list. "
        "You can reorder or remove items as needed."
    )

    # 4) DIAGNOSIS SEARCH INPUT
    search_input = st.text_input(
        "Type diagnosis:",
        value=st.session_state.search_input,
        key="diag_search"
    )
    st.session_state.search_input = search_input

    # All possible choices
    all_choices = [c.strip() for c in str(row.get("choices", "")).split(",")]
    # **Hide matches** until the user starts typing
    if st.session_state.search_input:
        matches = [
            c for c in all_choices
            if st.session_state.search_input.lower() in c.lower()
        ]
    else:
        matches = []

    if matches:
        st.write("Matching diagnoses:")
        for match in matches:
            # Only show button if not already selected
            if match not in st.session_state.selected_diagnoses:
                if st.button(f"➕ {match}", key=f"match_{match}"):
                    st.session_state.selected_diagnoses.append(match)
                    # If your Streamlit version is < 1.9 and lacks experimental_rerun, remove or adapt
                    st.rerun()

    # 5) SHOW SELECTED DIAGNOSES + UP/DOWN/REMOVE
    st.write("Your prioritized diagnoses:")
    arrow_up = "⬆️"
    arrow_down = "⬇️"
    trash_icon = "🗑️"

    for i, diag in enumerate(st.session_state.selected_diagnoses):
        col1, col2, col3, col4 = st.columns([6, 1, 1, 1])
        with col1:
            st.write(f"{i+1}. {diag}")
        with col2:
            if i > 0:
                if st.button(arrow_up, key=f"up_{i}"):
                    st.session_state.selected_diagnoses[i], st.session_state.selected_diagnoses[i-1] = \
                        st.session_state.selected_diagnoses[i-1], st.session_state.selected_diagnoses[i]
                    st.rerun()
        with col3:
            if i < len(st.session_state.selected_diagnoses) - 1:
                if st.button(arrow_down, key=f"down_{i}"):
                    st.session_state.selected_diagnoses[i], st.session_state.selected_diagnoses[i+1] = \
                        st.session_state.selected_diagnoses[i+1], st.session_state.selected_diagnoses[i]
                    st.rerun()
        with col4:
            if st.button(trash_icon, key=f"remove_{i}"):
                st.session_state.selected_diagnoses.pop(i)
                st.rerun()

    # 6) SUBMISSION: Only if exactly 3 are selected
    if len(st.session_state.selected_diagnoses) == 3 and not st.session_state.answered:
        if st.button("Submit Answer"):
            st.session_state.answered = True
            correct_order = [
                safe_text(row.get("answer", "")).strip(),
                safe_text(row.get("sec_dx", "")).strip(),
                safe_text(row.get("thir_dx", "")).strip(),
            ]
            user_order = [diag.strip() for diag in st.session_state.selected_diagnoses]
        
            # Create a DataFrame to display the results in a table.
            results_df = pd.DataFrame({
                "Rank": [1, 2, 3],
                "Your Answer": user_order,
                "Correct Answer": correct_order
            })
        
            st.write("**Your Prioritized Diagnosis:**")
            st.table(results_df)
            
            if user_order == correct_order:
                st.success("Correct!")
            else:
                st.error("Incorrect.")
                st.info(row.get("answer_explanation", ""))

            # Check if passcode is locked (one-time usage)
            locked = check_and_add_passcode(st.session_state.assigned_passcode)
            if not locked and not st.session_state.review_sent:
                filename = f"review_{st.session_state.user_name}_{row['record_id']}.docx"
                generate_review_doc_prioritized(row, user_order, filename)
                send_email_with_attachment(
                    to_emails=[st.session_state.recipient_email],
                    subject="Review of Incorrect Prioritized Diagnosis Answer",
                    body="Please find attached a review of your response.",
                    attachment_path=filename
                )
                st.session_state.review_sent = True

        st.success("Case complete. Thank you for your response. You may now close the window.")

    elif len(st.session_state.selected_diagnoses) != 3 and not st.session_state.answered:
        st.info(f"Please select exactly 3 diagnoses. You have selected {len(st.session_state.selected_diagnoses)}.")

    if st.session_state.answered:
        st.info("Case complete. Thank you for your response. You may now close the window.")

# Main app logic
def main():
    initialize_state()
    if not st.session_state.authenticated:
        login_screen()
    else:
        exam_screen_prioritized()

if __name__ == "__main__":
    main()

