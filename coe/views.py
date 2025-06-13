from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings
from datetime import datetime, timedelta
from django.contrib.auth.hashers import make_password, check_password
from pymongo import MongoClient
from io import TextIOWrapper
from django.views.decorators.http import require_POST
from django.core.files.storage import default_storage
from bson import ObjectId
from django.core.files.storage import FileSystemStorage
from dotenv import load_dotenv
from pymongo.errors import PyMongoError
import os
import json
import re
import pdfplumber
import shutil
import jwt
import tempfile
from pdf2image import convert_from_bytes
import google.generativeai as genai
import base64
import zipfile
import logging
import concurrent.futures
import threading
from queue import Queue
import uuid
import io
import boto3
import PIL.Image
import numpy as np
import cv2
from pdf2image import convert_from_path, pdfinfo_from_path
from io import BytesIO
import time  # Add this import for time.sleep()
import random  # Add this for potential jitter in retry logic
import os
import json
import smtplib
from io import BytesIO
from datetime import datetime
from bson import ObjectId
from email.mime.text import MIMEText
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import tempfile
import jwt
import csv
from datetime import datetime
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from bson.objectid import ObjectId


load_dotenv()

POPPLER_PATH = r"C:\Program Files\poppler-24.08.0\Library\bin"

def process_pdf_request(request):
    pdf_file = request.POST.get('pdf_path')
    info = pdfinfo_from_path(pdf_file, poppler_path=POPPLER_PATH)
    images = convert_from_path(pdf_file, poppler_path=POPPLER_PATH)
    return info, images


JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256' 

# Database connection
MONGO_URI = os.getenv("MONGO_URI")

# Global variables to track bulk processing jobs
bulk_upload_jobs = {}
evaluation_jobs = {}
MAX_WORKERS = 5  # Maximum parallel workers

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['COE']

admin_collection = db['admin']
student_collection = db["student"]
subadmin_collection = db["subadmin"]
exam_mapped_questions_collection = db["exam_mapped_questions"]
results_collection = db["results"]
answer_sheet_collection = db["answer_sheets"]
exam_collection = db["exam_details"]
semester_collection = db["semester"]
rubrics_collection = db["rubrics"]

# Setup Gemini API
genai.configure(api_key="AIzaSyCz-UDMdFCuC5bCempQgNuDTTcqzk7BY3E")
model = genai.GenerativeModel("gemini-2.0-flash")

session2 = genai.configure(api_key="AIzaSyAyYeY-j7BzvH_pLs-EB2iVobU4AXVFVIY")
model2 = genai.GenerativeModel("gemini-2.0-flash-lite")


AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME")
AWS_S3_CUSTOM_DOMAIN = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com/"

# S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_S3_REGION_NAME
)


#======================================================= FUNCTIONS ===========================================================================

def generate_tokens(admin_user, name):
    """Generates JWT tokens for admin authentication.

    Args:
        admin_user (str): The admin user ID.

    Returns:
        dict: A dictionary containing the JWT token.
    """
    payload = {
        'admin_user': str(admin_user),
        'name': name,
        'role': 'admin',
        "exp": datetime.utcnow() + timedelta(days=1),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {'jwt': token}

def upload_to_s3(file_obj, path):
    try:
        print(f"📤 Uploading to S3 → {path}")
        s3_client.upload_fileobj(file_obj, AWS_STORAGE_BUCKET_NAME, path)
        url = f"{AWS_S3_CUSTOM_DOMAIN}/{path}"
        print(f"✅ S3 Upload Success: {url}")
        return url
    except Exception as e:
        print(f"❌ S3 Upload failed for {path}: {str(e)}")
        return None


def parse_metadata(answer_block):
    """Extract CO, Bloom Level, Keywords, Answer Text from an answer block."""
    co = ""
    bloom = ""
    keywords = []
    answer_text = ""

    co_match = re.search(r'CO:\s*(\w+)', answer_block)
    bloom_match = re.search(r'Bloom[’\']?s Level:\s*(\w+)', answer_block)
    kw_match = re.search(r'Keywords:\s*(.+)', answer_block)
    answer_match = re.search(r'Answer[:]*\s*(.*)', answer_block, re.S)

    if co_match:
        co = co_match.group(1)
    if bloom_match:
        bloom = bloom_match.group(1)
    if kw_match:
        keywords = [x.strip() for x in kw_match.group(1).split(",")]
    if answer_match:
        answer_text = answer_match.group(1).strip()

    return co, bloom, keywords, answer_text

def parse_metadata_block(ans_block):
    """Extract CO, Bloom's Level, Keywords, Answer text from an answer block."""
    co = ""
    bloom = ""
    keywords = []
    answer_text = ""

    # First extract the metadata (CO, Bloom, Keywords)
    co_match = re.search(r'CO:\s*(\w+)', ans_block)
    bloom_match = re.search(r"Bloom[\'\"]?s Level:\s*(\w+)", ans_block)
    kw_match = re.search(r'Keywords:\s*(.+?)(?=\n|$)', ans_block)

    if co_match:
        co = co_match.group(1)
    if bloom_match:
        bloom = bloom_match.group(1)
    if kw_match:
        keywords = [x.strip() for x in kw_match.group(1).split(",")]
    
    # Handle different answer formats:
    # 1. "Expected Answer:" or "Answer:" followed by text
    # 2. "Answer Format: Paragraph" followed by text
    # 3. Answer text directly after metadata without explicit label
    
    # Try explicit answer patterns first
    answer_patterns = [
        # Regular answer label
        r'(?:Expected\s+)?Answer(?:\s*\(.*?\))?[:]\s*(.*?)(?=(?:\n\s*(?:Q\d|\(OR\)|CO:|$)))',
        # Answer with format specification
        r'Answer\s+Format:\s+\w+\s*(.*?)(?=(?:\n\s*(?:Q\d|\(OR\)|CO:|Keywords:|$)))',
        # Answer with word count specification
        r'Answer\s+\(\s*≈\d+.*?words\):\s*(.*?)(?=(?:\n\s*(?:Q\d|\(OR\)|CO:|$)))'
    ]
    
    for pattern in answer_patterns:
        answer_match = re.search(pattern, ans_block, re.DOTALL | re.IGNORECASE)
        if answer_match:
            # Get the raw answer text and clean it up
            raw_answer = answer_match.group(1).strip()
            
            # Handle bulleted lists by preserving bullet points
            bullet_pattern = re.compile(r'[\n\r]+\s*(?:•|-|\*)\s*', re.MULTILINE)
            formatted_answer = bullet_pattern.sub('\n• ', raw_answer)
            
            # Clean up excessive whitespace while preserving paragraph breaks
            answer_text = re.sub(r'\s*[\n\r]+\s*', '\n\n', formatted_answer).strip()
            return co, bloom, keywords, answer_text
    
    # If no explicit answer label was found, try to extract all text after the metadata sections
    metadata_sections = ["CO:", "Bloom", "Keywords:", "Maximum Marks:"]
    metadata_pattern = '|'.join(metadata_sections)
    section_matches = list(re.finditer(metadata_pattern, ans_block, re.IGNORECASE))
    
    if section_matches:
        # Find the last metadata section
        last_metadata_pos = 0
        for match in section_matches:
            # Find the end of this metadata line
            line_end = ans_block.find('\n', match.start())
            if line_end > last_metadata_pos:
                last_metadata_pos = line_end
        
        # Extract everything after the last metadata section
        if last_metadata_pos > 0:
            raw_answer = ans_block[last_metadata_pos:].strip()
            
            # Only use this if we have actual content (longer than 10 chars)
            if len(raw_answer) > 10:  # Fixed syntax error here
                # Handle bulleted lists
                bullet_pattern = re.compile(r'[\n\r]+\s*(?:•|-|\*)\s*', re.MULTILINE)
                formatted_answer = bullet_pattern.sub('\n• ', raw_answer)
                
                # Clean up whitespace
                answer_text = re.sub(r'\s*[\n\r]+\s*', '\n\n', formatted_answer).strip()
    
    return co, bloom, keywords, answer_text

def parse_pdfs(question_pdf_path, answer_pdf_path):
    questions = []

    # Extract question text
    with pdfplumber.open(question_pdf_path) as qpdf:
        qtext = ""
        for page in qpdf.pages:
            qtext += page.extract_text() + "\n"

    # Extract answer text
    with pdfplumber.open(answer_pdf_path) as apdf:
        atext = ""
        for page in apdf.pages:
            atext += page.extract_text() + "\n"

    print("\nDEBUG QUESTION TEXT:\n", qtext[:1000])
    print("\nDEBUG ANSWER TEXT:\n", atext[:1000])

    # Start from 1./Q1.
    q_start = re.search(r'(1[\.|\)]|Q1[\.|\)])', qtext)
    if q_start:
        qtext = qtext[q_start.start():]
    else:
        print("⚠️ Could not find 1. or Q1 in question paper!")

    # Identify Part A and Part B sections in answer key
    part_a_match = re.search(r'PART\s*[–\-\s]\s*A', atext, re.IGNORECASE)
    part_b_match = re.search(r'PART\s*[–\-\s]\s*B', atext, re.IGNORECASE)
    
    if part_a_match and part_b_match:
        part_a_text = atext[part_a_match.start():part_b_match.start()]
        part_b_text = atext[part_b_match.start():]
    else:
        part_a_text = atext
        part_b_text = ""
        print("⚠️ Could not clearly separate Part A and Part B in answer key!")

    # Split main questions in question paper
    q_main_splits = re.split(r'(\d+[\.|\)])', qtext)
    q_pairs = [(q_main_splits[i], q_main_splits[i+1]) for i in range(1, len(q_main_splits)-1, 2)]

    # Process Part A answers - using our specifically designed parser for the format
    part_a_questions = {}
    
    # Find all Q1, Q2, etc. sections in Part A
    q_pattern = re.compile(r'Q(\d+)[\.|\):]?\s*(.*?)(?=Q\d+[\.|\):]|PART\s*[–\-\s]\s*B|$)', re.DOTALL | re.IGNORECASE)
    for match in q_pattern.finditer(part_a_text):
        q_num = match.group(1)
        ans_text = match.group(2).strip()
        
        # Only process questions 1-5 as Part A
        if 1 <= int(q_num) <= 5:
            co, bloom, keywords, answer_text = parse_metadata_block(ans_text)
            part_a_questions[f"q{q_num}"] = {
                "co": co,
                "bloom": bloom,
                "keywords": keywords,
                "answer_text": answer_text
            }

    # Process Part B answers - this part needs special handling
    part_b_questions = {}
    
    # First, find all major question blocks in Part B (Q6, Q7, Q8)
    main_q_blocks = re.split(r'(Q\d+\s*\([ab]\)[\.:])', part_b_text)
    
    # Group blocks by question number
    current_question = None
    question_blocks = {}
    
    for block in main_q_blocks:
        q_header_match = re.match(r'Q(\d+)\s*\(([ab])\)[\.:]', block)
        if q_header_match:
            # This is a question header
            q_num = q_header_match.group(1)
            q_part = q_header_match.group(2)
            current_question = f"q{q_num}{q_part}"
            question_blocks[current_question] = ""
        elif current_question:
            # This is content for the current question
            question_blocks[current_question] += block
    
    # Process each question block
    for q_key, q_content in question_blocks.items():
        # Skip empty blocks
        if not q_content.strip():
            continue
            
        # Find the "CO:" line to determine the start of the metadata
        metadata_start = q_content.find("CO:")
        if metadata_start > 0:
            # Use a cleaner section of the content
            clean_content = q_content[metadata_start:].strip()
            co, bloom, keywords, answer_text = parse_metadata_block(clean_content)
            
            # Handle cases where answer_text is empty but there's content after "Answer:"
            if not answer_text:
                answer_match = re.search(r'Answer:\s*(.*)', q_content, re.DOTALL)
                if answer_match:
                    answer_text = answer_match.group(1).strip()
            
            part_b_questions[q_key] = {
                "co": co,
                "bloom": bloom,
                "keywords": keywords,
                "answer_text": answer_text
            }
    
    # Special handling for longer answer sections that might not be properly split
    # Look for more complex patterns like "Q6 (a). Demonstrate an understanding..."
    q_ab_detailed = re.compile(r'Q(\d+)\s*\(([ab])\)[\.:]?\s*(.*?)(?=\(OR\)|Q\d+\s*\([ab]\)[\.:]|$)', re.DOTALL)
    
    for match in q_ab_detailed.finditer(part_b_text):
        q_num = match.group(1)
        q_part = match.group(2)
        ans_text = match.group(3).strip()
        
        # Skip if already processed
        q_key = f"q{q_num}{q_part}"
        if q_key in part_b_questions and part_b_questions[q_key].get("answer_text"):
            continue
            
        # Process this answer block
        co_match = re.search(r'CO:\s*(\w+)', ans_text)
        bloom_match = re.search(r'Bloom[\'"]?s Level:\s*(\w+)', ans_text)
        
        co = co_match.group(1) if co_match else ""
        bloom = bloom_match.group(1) if bloom_match else ""
        
        # Find answer text - look for patterns
        answer_section = ""
        answer_match = re.search(r'Answer:\s*(.*)', ans_text, re.DOTALL)
        if answer_match:
            answer_section = answer_match.group(1).strip()
        
        # If can't find "Answer:" label, try to extract main content
        if not answer_section:
            lines = ans_text.split('\n')
            content_lines = []
            in_content = False
            
            for line in lines:
                # Skip metadata lines
                if re.search(r'(CO:|Bloom|Maximum Marks:|Keywords:)', line):
                    continue
                    
                # If we find a substantial line with real content, start capturing
                if len(line.strip()) > 30 and not in_content:
                    in_content = True
                
                if in_content:
                    content_lines.append(line)
            
            answer_section = '\n'.join(content_lines).strip()
        
        # Update or create entry
        if q_key not in part_b_questions:
            part_b_questions[q_key] = {
                "co": co,
                "bloom": bloom,
                "keywords": [],
                "answer_text": answer_section
            }
        elif not part_b_questions[q_key].get("answer_text"):
            part_b_questions[q_key]["answer_text"] = answer_section
    
    # Combine all answers
    answer_dict = {**part_a_questions, **part_b_questions}
    
    # Process each question from question paper and match with answers
    for idx, (qnum, qcontent) in enumerate(q_pairs):
        qnum_clean = qnum.strip('.)')
        
        # Determine part based on question number
        # Questions 1-5 are Part A, 6 and above are Part B
        part = "A" if idx < 5 else "B"

        # Assign marks based on part and question number
        if part == "A":
            marks = 2  # Part A questions are 2 marks each
        else:
            # Part B questions are either 13 or 14 marks based on question number
            if int(qnum_clean) == 8:  # Question 8 is 14 marks
                marks = 14
            else:  # Questions 6 and 7 are 13 marks each
                marks = 13

        # Extract bloom level and CO directly from question if available
        bloom_level = ""
        co = ""
        bloom_match = re.search(r'\b(REM|UND|APP|ANA|EVA|CRT)\b', qcontent, re.IGNORECASE)
        co_match = re.search(r'\b(CO\d+)\b', qcontent, re.IGNORECASE)
        
        if bloom_match:
            bloom_level = bloom_match.group(1).upper()
        if co_match:
            co = co_match.group(1).upper()

        # Clean up the question content - remove "PART - B" text
        qcontent = re.sub(r'PART\s*[-–]\s*B\s*(?:\(\d+\s*×\s*\d+\s*=\s*\d+\s*marks\))?', '', qcontent, flags=re.IGNORECASE).strip()

        # Split optional (a)/(b) in question paper
        opt_q_splits = re.split(r'\(\s*([abAB])\s*\)', qcontent)

        if len(opt_q_splits) > 1:
            # Has optional sub-questions (a)/(b)
            for opt_idx in range(1, len(opt_q_splits), 2):
                opt_label = opt_q_splits[opt_idx].lower()
                opt_text = opt_q_splits[opt_idx + 1].strip()

                # Clean up the sub-question text - remove any remaining "PART - B" text
                opt_text = re.sub(r'PART\s*[-–]\s*B\s*(?:\(\d+\s*×\s*\d+\s*=\s*\d+\\s*marks\))?', '', opt_text, flags=re.IGNORECASE).strip()

                # Use the same marks as determined by the part (A or B)
                subq_marks = marks

                q_label_key = f"q{qnum_clean}{opt_label}"
                ans_data = answer_dict.get(q_label_key, {})

                # Fallback to plain question number if we can't find the specific option
                if (not ans_data or not ans_data.get("answer_text")) and part == "B":
                    q_label_key = f"q{qnum_clean}"
                    ans_data = answer_dict.get(q_label_key, {})

                # Ensure keywords exist - if empty, generate based on the question and answer
                keywords = ans_data.get("keywords", [])
                if not keywords:
                    # Generate default keywords based on question number and topic
                    if part == "B":
                        if opt_label == "a":
                            if int(qnum_clean) == 6:
                                keywords = ["Mughal literature", "Persian influence", "Akbar", "cultural syncretism", "court patronage"]
                            elif int(qnum_clean) == 7:
                                keywords = ["Raja Ram Mohan Roy", "social reform", "education reform", "women's rights", "religious reform"]
                            elif int(qnum_clean) == 8:
                                keywords = ["Nyaya", "Vaisheshika", "Hindu philosophy", "metaphysics", "epistemology"]
                        elif opt_label == "b":
                            if int(qnum_clean) == 6:
                                keywords = ["Bhakti movement", "Tulsidas", "Braj Bhasha", "devotional poetry", "medieval literature"]
                            elif int(qnum_clean) == 7:
                                keywords = ["Swami Vivekananda", "Arya Samaj", "religious nationalism", "national identity", "reform movements"]
                            elif int(qnum_clean) == 8:
                                keywords = ["Yoga", "Samkhya", "Vedanta", "spiritual liberation", "Indian philosophical systems"]

                questions.append({
                    "question_no": f"{qnum_clean}{opt_label})",
                    "part": part,
                    "marks": subq_marks,
                    "question_text": opt_text,
                    "answer_text": ans_data.get("answer_text", ""),
                    "keywords": keywords,
                    "bloom_level": ans_data.get("bloom", "") or bloom_level,
                    "CO": ans_data.get("co", "") or co
                })
        else:
            # Normal question (no (a)/(b))
            q_label_key = f"q{qnum_clean}"
            ans_data = answer_dict.get(q_label_key, {})
            
            # Special handling for part B questions that might be merged or have variants
            if part == "B" and (not ans_data or not ans_data.get("answer_text")):
                # Try looking for a/b options in answer dict
                for suffix in ['a', 'b']:
                    alt_key = f"q{qnum_clean}{suffix}"
                    if alt_key in answer_dict and answer_dict[alt_key].get("answer_text"):
                        ans_data = answer_dict[alt_key]
                        break
            
            # Ensure keywords exist for normal questions too
            keywords = ans_data.get("keywords", [])
            if not keywords and part == "A":
                # Generate generic keywords for Part A questions
                keywords = [f"concept {qnum_clean}", f"topic {qnum_clean}", f"short answer {qnum_clean}"]
                
            questions.append({
                "question_no": qnum_clean,
                "part": part,
                "marks": marks,
                "question_text": qcontent.strip(),
                "answer_text": ans_data.get("answer_text", ""),
                "keywords": keywords,
                "bloom_level": ans_data.get("bloom", "") or bloom_level,
                "CO": ans_data.get("co", "") or co
            })

    return questions

@csrf_exempt
def get_subject_by_id(request, exam_id, subject_code):
    """
    Returns exam metadata and the specific subject details (name, code, question/answer URLs).
    """
    if request.method != "GET":
        return JsonResponse({'error': 'Invalid request method'}, status=405)

    try:
        exam = db["exam_details"].find_one({"_id": ObjectId(exam_id)})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        # Find the specific subject
        subject_code = subject_code.strip().upper()
        matching_subject = next(
            (s for s in exam.get("subjects", []) if s.get("subject_code", "").strip().upper() == subject_code),
            None
        )
        if not matching_subject:
            return JsonResponse({'error': f'Subject code {subject_code} not found in exam'}, status=404)

        # Format the subject response
        subject_data = {
            "subject_name": matching_subject.get("subject_name", ""),
            "subject_code": matching_subject.get("subject_code", ""),
            "question_paper": {
                "filename": matching_subject.get("question_paper", {}).get("filename", ""),
                "url": matching_subject.get("question_paper", {}).get("url", "")
            },
            "answer_key": {
                "filename": matching_subject.get("answer_key", {}).get("filename", ""),
                "url": matching_subject.get("answer_key", {}).get("url", "")
            }
        }

        # Build final exam data response
        exam_data = {
            "_id": str(exam["_id"]),
            "exam_type": exam.get("exam_type", ""),
            "college": exam.get("college", ""),
            "department": exam.get("department", ""),
            "year": exam.get("year", ""),
            "semester": exam.get("semester", ""),
            "section": exam.get("section", ""),
            "created_by": {
                "name": exam.get("created_by", {}).get("name", ""),
                "id": str(exam.get("created_by", {}).get("id", ""))
            },
            "created_at": str(exam.get("created_at", "")),
            "subjects": [subject_data]  
        }

        return JsonResponse({"exam": exam_data}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
@csrf_exempt
def get_exam_questions(request, exam_id, subject_code):
    """
    Get mapped questions for a specific exam subject.
    Used to check if questions exist before starting validation.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Find mapped questions for this exam/subject
        exam_mapped_doc = exam_mapped_questions_collection.find_one({
            "$or": [
                {"exam_id": exam_id, "subject_code": subject_code},
                {"exam_id": str(exam_id), "subject_code": subject_code}
            ]
        })
        
        if not exam_mapped_doc:
            return JsonResponse({"error": "No mapped questions found"}, status=404)
        
        questions = exam_mapped_doc.get("questions", [])
        if not questions:
            return JsonResponse({"error": "Questions array is empty"}, status=404)
        
        # Return a simplified version of the questions for quick checking
        simplified_questions = []
        for q in questions:
            simplified_questions.append({
                "question_no": q.get("question_no"),
                "marks": q.get("marks", 0),
                "bloom_level": q.get("bloom_level", "")
            })
            
        return JsonResponse({
            "exam_id": exam_id,
            "subject_code": subject_code,
            "question_count": len(questions),
            "questions": simplified_questions
        }, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)    
def process_answer_sheet_with_ai(pdf_path, exam_id, student_id):
    """
    Process a student's answer sheet PDF using Gemini AI with advanced filtering
    
    This function:
    1. Converts PDF to images with Poppler
    2. Filters out red ink (teacher marks)
    3. Extracts text using Gemini AI
    4. Processes and structures the answers
    
    Returns a JSON structure with extracted answers
    """
    try:
        # Import required modules
        from pdf2image import convert_from_path
        import PIL.Image
        import io
        import re
        import cv2
        import numpy as np
        import json
        import os
        import sys
        
        # Print detailed debug info to help with troubleshooting
        print(f"Starting answer sheet processing for PDF: {pdf_path}")
        print(f"Student ID: {student_id}, Exam ID: {exam_id}")
        print(f"Current working directory: {os.getcwd()}")
        print(f"Python version: {sys.version}")
        
        # Step 1: Process the PDF pages with red ink filtering
        def pdf_to_images(pdf_path):
            # HARDCODED POPPLER PATH - for Windows
            poppler_path = r'C:\Program Files\poppler-24.08.0\Library\bin'
            print(f"Using Poppler path: {poppler_path}")
            
            try:
                # First try with Poppler path
                pages = convert_from_path(pdf_path, poppler_path=poppler_path)
                print(f"Successfully converted PDF to {len(pages)} pages using poppler_path")
            except Exception as e:
                print(f"Failed with poppler_path, trying without it: {str(e)}")
                # Fallback to no path specification
                pages = convert_from_path(pdf_path)
                print(f"Successfully converted PDF to {len(pages)} pages without specifying poppler_path")
                
            processed_pages = []

            for i, page in enumerate(pages):
                print(f"Processing page {i+1}/{len(pages)}")
                # Convert PIL Image to numpy array for OpenCV
                img_np = np.array(page)

                # Convert to BGR for OpenCV
                img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

                # Filter out red ink using HSV color space
                hsv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2HSV)

                # Define red color range in HSV
                lower_red1 = np.array([0, 100, 100])
                upper_red1 = np.array([10, 255, 255])
                lower_red2 = np.array([160, 100, 100])
                upper_red2 = np.array([180, 255, 255])

                # Create masks for red color
                mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
                mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
                red_mask = cv2.bitwise_or(mask1, mask2)

                # Invert mask to get non-red areas
                non_red_mask = cv2.bitwise_not(red_mask)

                # Apply mask to keep only non-red pixels
                filtered_img = cv2.bitwise_and(img_cv, img_cv, mask=non_red_mask)

                # Convert back to RGB and then to PIL Image
                filtered_rgb = cv2.cvtColor(filtered_img, cv2.COLOR_BGR2RGB)
                filtered_pil = PIL.Image.fromarray(filtered_rgb)

                processed_pages.append(filtered_pil)

            return processed_pages
            
        # Step 2: Convert image to bytes
        def image_to_bytes(image):
            buffered = io.BytesIO()
            image.save(buffered, format="JPEG")
            return buffered.getvalue()
            
        # Step 3: Extract cleaned text from each page using Gemini
        def extract_text_from_image(image_bytes, page_num, total_pages):
            image = PIL.Image.open(io.BytesIO(image_bytes))

            # Enhanced prompt for ML content detection
            prompt = """
            Extract all the text exactly as written in the image, focusing ONLY on handwritten content in blue or black ink.
            This answer sheet likely contains responses about machine learning, computer science, or technical topics.
            
            VERY IMPORTANT:
            1. Preserve question numbers exactly as they appear in the image (e.g. '1', '2', '3', '8b)', etc.)
            2. Keep the original formatting including bullet points, indentation, and line breaks.
            3. Ignore any text in red ink (teacher's marks).
            4. Capture all technical terms correctly (e.g. 'Linear Regression', 'TensorFlow', 'PyTorch').
            5. Don't expand abbreviations or correct technical terms - preserve them exactly as written.
            6. Do not ignore or omit any handwritten text - capture everything.
            """

            try:
                response = model.generate_content([prompt, image])
                raw_text = response.text.strip()
                print(f"Successfully extracted text from page {page_num}")
            except Exception as e:
                print(f"Error extracting text from page {page_num}: {str(e)}")
                # Try again with a simpler prompt as fallback
                fallback_prompt = "Extract all handwritten text from this image exactly as written, preserving question numbers."
                try:
                    response = model.generate_content([fallback_prompt, image])
                    raw_text = response.text.strip()
                    print(f"Successfully extracted text with fallback prompt")
                except Exception as e2:
                    print(f"Fallback extraction also failed: {str(e2)}")
                    return f"EXTRACTION_ERROR: {str(e2)}"

            # --- Clean-up phase ---
            # Remove printed headers/footers
            cleaned = re.sub(r'^\s*(PART-[A-C])\s*$', '', raw_text, flags=re.MULTILINE)
            cleaned = re.sub(r'\b(PART-[A-C])\b', '', cleaned)

            # Remove page numbers
            cleaned = re.sub(r'^\s*(Page\s*)?\d+\s*$', '', cleaned, flags=re.MULTILINE)

            # Remove institution names
            cleaned = re.sub(r'\bSNSCE\b|\bSNS\b', '', cleaned, flags=re.IGNORECASE)

            # Normalize line breaks
            cleaned = re.sub(r'\n\s*\n', '\n\n', cleaned)

            print(f"Cleaned text from page {page_num}:", cleaned[:200] + "..." if len(cleaned) > 200 else cleaned)
            return cleaned.strip()
            
        # Step 4: Process the text based on page content - specialized for ML exams
        def process_page_content(all_texts):
            # Combine all texts
            complete_text = "\n".join(all_texts)
            
            # Print the complete text for debugging
            print("Complete extracted text:", complete_text[:500] + "..." if len(complete_text) > 500 else complete_text)
            
            # Extract all questions and answers using regex patterns
            answers = []
            
            # Step 1: First check for part questions with a), b) format in the answers
            part_pattern = r'(?:^|\n)(\d+)(?:[\.\)])\s*([a-zA-Z]\))\s*(.*?)(?=(?:\n\s*\d+[\.\)]|\n\s*\d+[a-zA-Z]\)|\Z))'
            for match in re.finditer(part_pattern, complete_text, re.DOTALL):
                q_num = match.group(1)
                q_part = match.group(2)
                q_text = match.group(3).strip()
                
                # Create the proper format for the question number: "8b)"
                q_id = f"{q_num}{q_part}"
                
                # Only add if we have actual content
                if len(q_text) > 3:  # Avoid empty or very short answers
                    answers.append({
                        "question_no": q_id,
                        "answer_text": q_text
                    })
            
            # Step 2: For standalone questions where b) might be in the answer text
            # Pattern for questions like "6. b) Predicting Cricket Scores..."
            sub_in_text_pattern = r'(?:^|\n)(\d+)(?:[\.\)])\s*(?:[a-zA-Z]\))?\s*(.*?)(?=(?:\n\s*\d+[\.\)]|\Z))'
            for match in re.finditer(sub_in_text_pattern, complete_text, re.DOTALL):
                q_num = match.group(1)
                q_text = match.group(2).strip()
                
                # Skip if this question number already exists with a part
                if any(a["question_no"] == f"{q_num}a)" or a["question_no"] == f"{q_num}b)" for a in answers):
                    continue
                
                # Check if the answer text starts with "a)" or "b)"
                part_in_text = re.match(r'^\s*([a-zA-Z]\))\s*(.*)', q_text)
                if part_in_text:
                    # Case where "b)" is in the answer text
                    part = part_in_text.group(1)
                    part_text = part_in_text.group(2).strip()
                    q_id = f"{q_num}{part}"
                    
                    if len(part_text) > 3:
                        answers.append({
                            "question_no": q_id,
                            "answer_text": part_text
                        })
                else:
                    # Regular question without part indicator
                    if len(q_text) > 3:
                        answers.append({
                            "question_no": q_num,
                            "answer_text": q_text
                        })
            
            # Sort answers by question number
            try:
                # For numbering like "1", "2", "8b)"
                def sort_key(item):
                    q = item["question_no"]
                    match = re.match(r'(\d+)([a-zA-Z]\)?)?', q)
                    if match:
                        num = int(match.group(1))
                        sub = match.group(2) if match.group(2) else ""
                        return (num, sub)
                    return (999, "")  # Default for unexpected formats
                
                answers.sort(key=sort_key)
            except Exception as sort_err:
                print(f"Error sorting answers: {sort_err}")
                # If sorting fails, at least keep the answers we found
            
            # Ensure we don't have duplicates
            unique_answers = []
            seen_question_nos = set()
            for ans in answers:
                if ans["question_no"] not in seen_question_nos:
                    unique_answers.append(ans)
                    seen_question_nos.add(ans["question_no"])
            
            return unique_answers            
        # Main execution starts here
        print(f"Processing PDF: {pdf_path}")
        
        # Convert PDF to filtered images
        pages = pdf_to_images(pdf_path)
        print(f"Successfully converted PDF to {len(pages)} pages")
        
        all_texts = []
        
        # Process each page and extract text
        for i, page in enumerate(pages):
            print(f"📄 Processing page {i + 1}...")
            image_bytes = image_to_bytes(page)
            text = extract_text_from_image(image_bytes, i + 1, len(pages))
            all_texts.append(text)
            
            # Print exactly like in Colab
            print(f"\n🔍 Page {i + 1} Extracted Text:\n{'-' * 40}\n{text}\n")
        
        # Process the extracted text to structured answers
        answers = process_page_content(all_texts)
        output = {"answers": answers}
        
        # Print exactly like in Colab
        print(f"\n\n🎯 Final Extracted Answers (Formatted JSON):\n")
        print(json.dumps(output, indent=2))
        print(f"Successfully extracted {len(answers)} answers from the PDF")
        
        # Final validation to ensure we return something useful
        if not answers:
            print("WARNING: No answers were extracted. Attempting emergency fallback...")
            # Emergency fallback - create basic structure from text
            fallback_answers = []
            for i, text in enumerate(all_texts):
                if len(text) > 20 and "EXTRACTION_ERROR" not in text:
                    q_num = str(i + 1)
                    fallback_answers.append({
                        "question_no": q_num,
                        "answer_text": text[:500]  # Limit length in fallback mode
                    })
            
            if fallback_answers:
                print(f"Created {len(fallback_answers)} fallback answers from raw text")
                return {"answers": fallback_answers}
        
        return output
        
    except Exception as e:
        print(f"Critical error in process_answer_sheet_with_ai: {str(e)}")
        # Return minimal structure rather than empty to prevent downstream errors
        return {"answers": [{"question_no": "ERR", "answer_text": f"Error processing answer sheet: {str(e)}"}]}

   
#======================================================= EXAM DETAILS ===========================================================================

def send_upload_error_email(subject, message,to_email):
    try:
        server = smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT)
        server.ehlo()
        server.starttls()
        server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)

        msg = MIMEText(message)
        msg['Subject'] = subject
        msg['From'] = settings.EMAIL_HOST_USER
        msg['To'] = to_email  

        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"[Email Error] Failed to send error notification: {e}")
        

# @csrf_exempt
# def append_subjects_to_exam(request):
#     print(request.POST)  
#     if request.method != 'POST':
#         return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

#     try:
#         exam_id = request.POST.get('exam_id')
#         if not exam_id:
#             return JsonResponse({'error': 'Missing exam_id in request'}, status=400)
        
#         exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
#         if not exam:
#             return JsonResponse({'error': 'Exam not found'}, status=404)

#         subjects_meta = json.loads(request.POST.get('subjects_meta', '[]'))
#         print("Received subjects_meta:", subjects_meta)  # Debug log
#         if not subjects_meta:
#             return JsonResponse({'error': 'Missing subjects metadata'}, status=400)

#         new_subjects = []

#         for subject in subjects_meta:
#             subject_name = subject.get('subject_name')
#             subject_code = subject.get('subject_code')
#             rubrics_text = subject.get('rubrics', '')  # Get rubrics text from subjects_meta
#             exam_date = subject.get('examDate', '')  # Get examDate from subjects_meta
#             session = subject.get('session', '')  # Get session from subjects_meta

#             if not subject_name or not subject_code:
#                 return JsonResponse({'error': 'Subject name and code required'}, status=400)

#             question_file = request.FILES.get(f"{subject_code}_question_paper")
#             answer_file = request.FILES.get(f"{subject_code}_answer_key")

#             if not question_file or not answer_file:
#                 return JsonResponse({'error': f'Missing files for subject {subject_code}'}, status=400)

#             for f in [question_file, answer_file]:
#                 if f and f.size > 10 * 1024 * 1024:
#                     return JsonResponse({'error': f'{f.name} exceeds 10MB'}, status=400)

#             # Read files once and reuse
#             q_bytes = question_file.read()
#             a_bytes = answer_file.read()

#             # Upload to S3
#             q_filename = f"{exam_id}_{subject_code}_qp.pdf"
#             a_filename = f"{exam_id}_{subject_code}_ak.pdf"
#             q_url = upload_to_s3(BytesIO(q_bytes), f"questionpapers/{q_filename}")
#             a_url = upload_to_s3(BytesIO(a_bytes), f"answerkeys/{a_filename}")

#             subject_obj = {
#                 'subject_name': subject_name,
#                 'subject_code': subject_code,
#                 'question_paper': {'filename': q_filename, 'url': q_url},
#                 'answer_key': {'filename': a_filename, 'url': a_url},
#                 'rubrics': rubrics_text,  # Store rubrics as text
#                 'examDate': exam_date,  # Store examDate
#                 'session': session,  # Store session
#             }

#             # Save parsed questions
#             try:
#                 temp_dir = tempfile.gettempdir()
#                 q_path = os.path.join(temp_dir, q_filename)
#                 a_path = os.path.join(temp_dir, a_filename)

#                 with open(q_path, "wb") as qf: qf.write(q_bytes)
#                 with open(a_path, "wb") as af: af.write(a_bytes)

#                 parsed_questions = parse_pdfs(q_path, a_path)
#                 print(len(parsed_questions), "questions parsed successfully")

#                 exam_mapped_questions_collection.insert_one({
#                     "exam_id": exam_id,
#                     "subject_code": subject_code,
#                     "questions": parsed_questions,
#                     "created_at": datetime.now()
#                 })

#             except Exception as e:
#                 error_msg = f"Failed to parse PDFs for {subject_code}: {str(e)}"
#                 send_upload_error_email(f"Error parsing subject {subject_name}", error_msg, exam['created_by']['id'])
#                 return JsonResponse({'error': error_msg}, status=500)

#             new_subjects.append(subject_obj)

#         # Update exam doc
#         exam_collection.update_one(
#             {'_id': ObjectId(exam_id)},
#             {'$push': {'subjects': {'$each': new_subjects}}}
#         )

#         return JsonResponse({
#             'message': 'Subjects appended successfully.',
#             'subjects_added': [s['subject_code'] for s in new_subjects]
#         }, status=200)

#     except Exception as e:
#         return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def append_subjects_to_exam(request):
    print("Received POST request:", request.POST)
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        exam_id = request.POST.get('exam_id')
        if not exam_id:
            return JsonResponse({'error': 'Missing exam_id in request'}, status=400)

        exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        subjects_meta = json.loads(request.POST.get('subjects_meta', '[]'))
        print("Received subjects_meta:", subjects_meta)  # Debug log
        if not subjects_meta:
            return JsonResponse({'error': 'Missing subjects metadata'}, status=400)

        new_subjects = []
        updated_subjects = []

        for subject in subjects_meta:
            subject_name = subject.get('subject_name')
            subject_code = subject.get('subject_code')
            rubrics_text = subject.get('rubrics', '')  # Get rubrics text from subjects_meta
            exam_date = subject.get('examDate', '')  # Get examDate from subjects_meta
            session = subject.get('session', '')  # Get session from subjects_meta

            if not subject_name or not subject_code:
                return JsonResponse({'error': 'Subject name and code required'}, status=400)

            question_file = request.FILES.get(f"{subject_code}_question_paper")
            answer_file = request.FILES.get(f"{subject_code}_answer_key")

            if not question_file or not answer_file:
                return JsonResponse({'error': f'Missing files for subject {subject_code}'}, status=400)

            for f in [question_file, answer_file]:
                if f and f.size > 10 * 1024 * 1024:
                    return JsonResponse({'error': f'{f.name} exceeds 10MB'}, status=400)

            # Read files once and reuse
            q_bytes = question_file.read()
            a_bytes = answer_file.read()

            # Upload to S3
            q_filename = f"{exam_id}_{subject_code}_qp.pdf"
            a_filename = f"{exam_id}_{subject_code}_ak.pdf"
            q_url = upload_to_s3(BytesIO(q_bytes), f"questionpapers/{q_filename}")
            a_url = upload_to_s3(BytesIO(a_bytes), f"answerkeys/{a_filename}")

            subject_obj = {
                'subject_name': subject_name,
                'subject_code': subject_code,
                'question_paper': {'filename': q_filename, 'url': q_url},
                'answer_key': {'filename': a_filename, 'url': a_url},
                'rubrics': rubrics_text,  # Store rubrics as text
                'examDate': exam_date,  # Store examDate
                'session': session,  # Store session
            }

            # Save parsed questions
            try:
                temp_dir = tempfile.gettempdir()
                q_path = os.path.join(temp_dir, q_filename)
                a_path = os.path.join(temp_dir, a_filename)

                with open(q_path, "wb") as qf: qf.write(q_bytes)
                with open(a_path, "wb") as af: af.write(a_bytes)

                parsed_questions = parse_pdfs(q_path, a_path)
                print(len(parsed_questions), "questions parsed successfully")

                # Add exam_type to the exam_mapped_questions document
                exam_mapped_questions_collection.insert_one({
                    "exam_id": exam_id,
                    "exam_type": exam.get("exam_type", ""),  # Add exam_type from exam_collection
                    "subject_code": subject_code,
                    "questions": parsed_questions,
                    "created_at": datetime.now()
                })

            except Exception as e:
                error_msg = f"Failed to parse PDFs for {subject_code}: {str(e)}"
                send_upload_error_email(f"Error parsing subject {subject_name}", error_msg, exam['created_by']['id'])
                return JsonResponse({'error': error_msg}, status=500)

            # Check if subject already exists
            existing_subject = next((s for s in exam['subjects'] if s['subject_code'] == subject_code or s['subject_name'] == subject_name), None)

            if existing_subject:
                # Update existing subject
                exam_collection.update_one(
                    {'_id': ObjectId(exam_id), 'subjects.subject_code': subject_code},
                    {'$set': {'subjects.$': subject_obj}}
                )
                updated_subjects.append(subject_obj)
            else:
                # Append new subject
                new_subjects.append(subject_obj)

        # Update exam doc with new subjects
        if new_subjects:
            exam_collection.update_one(
                {'_id': ObjectId(exam_id)},
                {'$push': {'subjects': {'$each': new_subjects}}}
            )

        return JsonResponse({
            'message': 'Subjects processed successfully.',
            'subjects_added': [s['subject_code'] for s in new_subjects],
            'subjects_updated': [s['subject_code'] for s in updated_subjects]
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# @csrf_exempt
# def append_subjects_to_exam_without_files(request):
#     print(request.POST)  
#     if request.method != 'POST':
#         return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

#     try:
#         exam_id = request.POST.get('exam_id')
#         if not exam_id:
#             return JsonResponse({'error': 'Missing exam_id in request'}, status=400)
        
#         exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
#         if not exam:
#             return JsonResponse({'error': 'Exam not found'}, status=404)

#         subjects_meta = json.loads(request.POST.get('subjects_meta', '[]'))
#         print("Received subjects_meta:", subjects_meta)  # Debug log
#         if not subjects_meta:
#             return JsonResponse({'error': 'Missing subjects metadata'}, status=400)

#         new_subjects = []

#         for subject in subjects_meta:
#             subject_name = subject.get('subject_name')
#             subject_code = subject.get('subject_code')
#             rubrics_text = subject.get('rubrics', '')  # Get rubrics text from subjects_meta
#             exam_date = subject.get('examDate', '')  # Get examDate from subjects_meta
#             session = subject.get('session', '')  # Get session from subjects_meta

#             if not subject_name or not subject_code:
#                 return JsonResponse({'error': 'Subject name and code required'}, status=400)

#             # question_file = request.FILES.get(f"{subject_code}_question_paper")
#             # answer_file = request.FILES.get(f"{subject_code}_answer_key")

#             # if not question_file or not answer_file:
#             #     return JsonResponse({'error': f'Missing files for subject {subject_code}'}, status=400)

#             # for f in [question_file, answer_file]:
#             #     if f and f.size > 10 * 1024 * 1024:
#             #         return JsonResponse({'error': f'{f.name} exceeds 10MB'}, status=400)

#             # # Read files once and reuse
#             # q_bytes = question_file.read()
#             # a_bytes = answer_file.read()

#             # # Upload to S3
#             # q_filename = f"{exam_id}_{subject_code}_qp.pdf"
#             # a_filename = f"{exam_id}_{subject_code}_ak.pdf"
#             # q_url = upload_to_s3(BytesIO(q_bytes), f"questionpapers/{q_filename}")
#             # a_url = upload_to_s3(BytesIO(a_bytes), f"answerkeys/{a_filename}")

#             subject_obj = {
#                 'subject_name': subject_name,
#                 'subject_code': subject_code,
#                 # 'question_paper': {'filename': q_filename, 'url': q_url},
#                 # 'answer_key': {'filename': a_filename, 'url': a_url},
#                 'rubrics': rubrics_text,  # Store rubrics as text
#                 'examDate': exam_date,  # Store examDate
#                 'session': session,  # Store session
#             }

#             # # Save parsed questions
#             # try:
#             #     temp_dir = tempfile.gettempdir()
#             #     q_path = os.path.join(temp_dir, q_filename)
#             #     a_path = os.path.join(temp_dir, a_filename)

#             #     with open(q_path, "wb") as qf: qf.write(q_bytes)
#             #     with open(a_path, "wb") as af: af.write(a_bytes)

#                 # parsed_questions = parse_pdfs(q_path, a_path)
#                 # print(len(parsed_questions), "questions parsed successfully")

#                 # exam_mapped_questions_collection.insert_one({
#                 #     "exam_id": exam_id,
#                 #     "subject_code": subject_code,
#                 #     "questions": parsed_questions,
#                 #     "created_at": datetime.now()
#                 # })

#             # except Exception as e:
#             #     error_msg = f"Failed to parse PDFs for {subject_code}: {str(e)}"
#             #     send_upload_error_email(f"Error parsing subject {subject_name}", error_msg, exam['created_by']['id'])
#             #     return JsonResponse({'error': error_msg}, status=500)

#             new_subjects.append(subject_obj)

#         # Update exam doc
#         exam_collection.update_one(
#             {'_id': ObjectId(exam_id)},
#             {'$push': {'subjects': {'$each': new_subjects}}}
#         )

#         return JsonResponse({
#             'message': 'Subjects appended successfully.',
#             'subjects_added': [s['subject_code'] for s in new_subjects]
#         }, status=200)

#     except Exception as e:
#         return JsonResponse({'error': str(e)}, status=500)



def save_subjects_to_sem_examy(obj_id):

    """
    Save the subjects of a given semester document to all matching exams.

    Given the ObjectId of a semester document, this function will fetch the
    semester document and find all matching exams in the exam collection.
    It will then update the subjects of all matching exams with the subjects
    from the semester document.

    Args:
        obj_id (ObjectId): The ObjectId of the semester document

    Returns:
        None
    """
    sem_exam = semester_collection.find_one({'_id': ObjectId(obj_id)})
    if not sem_exam:
        print(f"No semester document found with _id: {obj_id}")
        return

    query = {
        'department': sem_exam['department'],
        'year': sem_exam['year'],
        'semester': sem_exam['semester'],
        'batch': sem_exam['batch']
    }
    exams = list(exam_collection.find(query))
    if not exams:
        print(f"No exams found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return

    subjects = sem_exam.get('subjects', [])
    if not subjects:
        print(f"No subjects found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return
    


    # Update all matching exams
    result = exam_collection.update_many(
        query,
        {'$set': {'subjects': subjects}}
    )




    print(f"Updated {result.modified_count} exam(s) with new subjects for semester {sem_exam['semester']} in {sem_exam['department']} department")
    return

def save_subjects_to_sem_examx(obj_id):

    """
    Save the subjects of a given semester document to all matching exams.

    Given the ObjectId of a semester document, this function will fetch the
    semester document and find all matching exams in the exam collection.
    It will then update the subjects of all matching exams with the subjects
    from the semester document.

    Args:
        obj_id (ObjectId): The ObjectId of the semester document

    Returns:
        None
    """
    sem_exam = semester_collection.find_one({'_id': ObjectId(obj_id)})
    if not sem_exam:
        print(f"No semester document found with _id: {obj_id}")
        return

    query = {
        'department': sem_exam['department'],
        'year': sem_exam['year'],
        'semester': sem_exam['semester'],
        'batch': sem_exam['batch']
    }
    exams = list(exam_collection.find(query))
    if not exams:
        print(f"No exams found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return

    subjects = sem_exam.get('subjects', [])
    if not subjects:
        print(f"No subjects found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return
    


    # Update all matching exams
    result = exam_collection.update_many(
        query,
        {'$set': {'subjects': subjects}}
    )




    print(f"Updated {result.modified_count} exam(s) with new subjects for semester {sem_exam['semester']} in {sem_exam['department']} department")
    return



from bson import ObjectId

from bson import ObjectId

def save_subjects_to_sem_exam(obj_id):
    """
    Update the subjects of all matching exams with the subjects from a given semester document,
    preserving additional details like question_paper, answer_key, rubrics, examDate, and session.

    Given the ObjectId of a semester document, this function will fetch the
    semester document and find all matching exams in the exam collection.
    It will then update the subjects of all matching exams with the subjects
    from the semester document, preserving additional details.

    Args:
        obj_id (ObjectId): The ObjectId of the semester document

    Returns:
        None
    """
    # Fetch the semester document
    sem_exam = semester_collection.find_one({'_id': ObjectId(obj_id)})
    if not sem_exam:
        print(f"No semester document found with _id: {obj_id}")
        return

    # Create a query to find matching exams
    query = {
        'department': sem_exam['department'],
        'year': sem_exam['year'],
        'semester': sem_exam['semester'],
        'batch': sem_exam['batch']
    }

    # Fetch all matching exams
    exams = list(exam_collection.find(query))
    if not exams:
        print(f"No exams found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return

    # Get subjects from the semester document
    subjects = sem_exam.get('subjects', [])
    if not subjects:
        print(f"No subjects found for semester {sem_exam['semester']} in {sem_exam['department']} department")
        return

    # Update each exam document
    for exam in exams:
        exam_id = exam['_id']
        exam_subjects = exam.get('subjects', [])
        updated_subjects = []

        # Create a set of subject codes in the exam for quick lookup
        exam_subject_codes = {subj['subject_code'] for subj in exam_subjects}

        # Preserve existing subjects and their details, updating only matching ones
        for exam_subject in exam_subjects:
            for sem_subject in subjects:
                if exam_subject['subject_code'] == sem_subject['subject_code']:
                    # Update subject_name, preserve other fields
                    updated_subject = exam_subject.copy()  # Preserve existing fields
                    updated_subject['subject_name'] = sem_subject['subject_name']
                    updated_subjects.append(updated_subject)
                    break
            else:
                # If no match, keep the existing subject as is
                updated_subjects.append(exam_subject)
                # print("updated_subjects", updated_subjects)

        # Add new subjects from semester that are not in the exam
        for sem_subject in subjects:
            if sem_subject['subject_code'] not in exam_subject_codes:
                # Add new subject with default values for additional fields
                new_subject = {
                    'subject_name': sem_subject['subject_name'],
                    'subject_code': sem_subject['subject_code'],
                    'question_paper': {'filename': '', 'url': ''},
                    'answer_key': {'filename': '', 'url': ''},
                    'rubrics': '',
                    'examDate': '',  # Default, adjust as needed
                    'session': ''    # Default, adjust as needed
                }
                updated_subjects.append(new_subject)

        # Update the exam document with the complete subjects list
        result = exam_collection.update_one(
            {'_id': ObjectId(exam_id)},
            {'$set': {'subjects': updated_subjects}}
        )
        print(f"Updated exam {exam_id} with {result.modified_count} modifications")

    print(f"Updated exams with new subjects for semester {sem_exam['semester']} in {sem_exam['department']} department")


@csrf_exempt
def append_subjects_to_exam_without_files(request):
    """
    Appends subjects to an existing exam without uploading any files. The subjects metadata is
    expected in the request body as a JSON array of objects, each containing the following
    properties: subject_name, subject_code, rubrics, examDate, and session.

    The endpoint also updates the semester collection document with the new subjects
    and calls save_subjects_to_sem_exam to update all matching exams with the new subjects.

    :return: A JSON response with a message and a list of subject codes that were appended
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:

        #for semester collection
        department = request.POST.get('department')
        year = request.POST.get('year')
        semester = request.POST.get('semester')
        batch = request.POST.get('batch')

        query = {
        'department': department,
        'year': year,
        'semester': semester,
        'batch': batch
        }

        sem_data = semester_collection.find_one(query)
        if not sem_data:
            return JsonResponse({'error': 'Semester data not found for the given parameters'}, status=404)
        

        
        #end semester collection




        exam_id = request.POST.get('exam_id')
        if not exam_id:
            return JsonResponse({'error': 'Missing exam_id in request'}, status=400)
        
        exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        subjects_meta = json.loads(request.POST.get('subjects_meta', '[]'))
        print("Received subjects_meta:", subjects_meta)  # Debug log
        if not subjects_meta:
            return JsonResponse({'error': 'Missing subjects metadata'}, status=400)

        new_subjects = []
        for_sem_subjects = []

        for subject in subjects_meta:
            subject_name = subject.get('subject_name')
            subject_code = subject.get('subject_code')
            rubrics_text = subject.get('rubrics', '')  # Get rubrics text from subjects_meta
            exam_date = subject.get('examDate', '')  # Get examDate from subjects_meta
            session = subject.get('session', '')  # Get session from subjects_meta

            if not subject_name or not subject_code:
                return JsonResponse({'error': 'Subject name and code required'}, status=400)

            # question_file = request.FILES.get(f"{subject_code}_question_paper")
            # answer_file = request.FILES.get(f"{subject_code}_answer_key")

            # if not question_file or not answer_file:
            #     return JsonResponse({'error': f'Missing files for subject {subject_code}'}, status=400)

            # for f in [question_file, answer_file]:
            #     if f and f.size > 10 * 1024 * 1024:
            #         return JsonResponse({'error': f'{f.name} exceeds 10MB'}, status=400)


            




            # # Read files once and reuse
            # q_bytes = question_file.read()
            # a_bytes = answer_file.read()

            # # Upload to S3
            # q_filename = f"{exam_id}_{subject_code}_qp.pdf"
            # a_filename = f"{exam_id}_{subject_code}_ak.pdf"
            # q_url = upload_to_s3(BytesIO(q_bytes), f"questionpapers/{q_filename}")
            # a_url = upload_to_s3(BytesIO(a_bytes), f"answerkeys/{a_filename}")

            subject_obj = {
                'subject_name': subject_name,
                'subject_code': subject_code,

                'rubrics': rubrics_text,  # Store rubrics as text
                'examDate': exam_date,  # Store examDate
                'session': session,  # Store session
            }

            for_sem_subjects_obj = {
                                'subject_name': subject_name,
                                'subject_code': subject_code,
            }



            new_subjects.append(subject_obj)
            for_sem_subjects.append(for_sem_subjects_obj)

        # Update exam doc
        exam_collection.update_one(
            {'_id': ObjectId(exam_id)},
            {'$push': {'subjects': {'$each': new_subjects}}}
        )

        # Update semester collection with new subjects
        sem_update_result = semester_collection.update_one(
            query,
            {'$push': {'subjects': {'$each': for_sem_subjects}}}
        )

        # Use the _id of the semester document for save_subjects_to_sem_exam
        sem_data = semester_collection.find_one(query)
        if sem_data:
            save_subjects_to_sem_exam(sem_data['_id'])

        return JsonResponse({
            'message': 'Subjects appended successfully.',
            'subjects_added': [s['subject_code'] for s in new_subjects]
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def edit_subjects_to_exam_without_files(request):  
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get('exam_id')
        subject_code = data.get('subject_code')
        subject_name = data.get('subject_name')
        rubrics = data.get('rubrics', '')
        exam_date = data.get('examDate', '')
        session = data.get('session', '')

        if not exam_id or not subject_code:
            return JsonResponse({'error': 'Missing exam_id or subject_code in request'}, status=400)

        exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        # Update the subject in the exam document
        exam_collection.update_one(
            {'_id': ObjectId(exam_id), 'subjects.subject_code': subject_code},
            {'$set': {
                'subjects.$.subject_name': subject_name,
                'subjects.$.rubrics': rubrics,
                'subjects.$.examDate': exam_date,
                'subjects.$.session': session,
            }}
        )

        # Update the subject in the semester collection
        department = exam.get('department')
        year = exam.get('year')
        semester = exam.get('semester')
        batch = exam.get('batch')

        query = {
            'department': department,
            'year': year,
            'semester': semester,
            'batch': batch
        }

        sem_data = semester_collection.find_one(query)
        if sem_data:
            semester_collection.update_one(
                {'_id': sem_data['_id'], 'subjects.subject_code': subject_code},
                {'$set': {
                    'subjects.$.subject_name': subject_name,
                    'subjects.$.rubrics': rubrics,
                    'subjects.$.examDate': exam_date,
                    'subjects.$.session': session,
                }}
            )
            # exam_collection.update_many(query, 
            #     {'$set': {
            #     'subjects.$.subject_name': subject_name,
            #     'subjects.$.rubrics': rubrics,
            #     'subjects.$.examDate': exam_date,
            #     'subjects.$.session': session,
            # }})

            # Call save_subjects_to_sem_exam to update all matching exams
            save_subjects_to_sem_exam(sem_data['_id'])

        return JsonResponse({'message': 'Subject updated successfully'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)



@csrf_exempt
def delete_subjects_to_exam_without_files(request):  
    """
    Delete a subject from an exam without files.

    Given the ObjectId of an exam and a subject code, this function will
    remove the subject from the exam document and from the matching semester
    document in the semester collection. It will also call
    save_subjects_to_sem_exam to update all matching exams.

    Args:
        request (HttpRequest): The request containing the exam_id and
            subject_code in the request body.

    Returns:
        JsonResponse: A JSON response with a message indicating the result
            of the deletion operation.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get('exam_id')
        subject_code = data.get('subject_code')

        if not exam_id or not subject_code:
            return JsonResponse({'error': 'Missing exam_id or subject_code in request'}, status=400)

        exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        # # Remove the subject from the exam document
        # exam_collection.update_one(
        #     {'_id': ObjectId(exam_id)},
        #     {'$pull': {'subjects': {'subject_code': subject_code}}}
        # )

        # Remove the subject from the semester collection
        department = exam.get('department')
        year = exam.get('year')
        semester = exam.get('semester')
        batch = exam.get('batch')

        query = {
            'department': department,
            'year': year,
            'semester': semester,
            'batch': batch
        }

        sem_data = semester_collection.find_one(query)
        if sem_data:
            semester_collection.update_one(
                {'_id': sem_data['_id']},
                {'$pull': {'subjects': {'subject_code': subject_code}}}
            )

            # Remove the subject from the exam document

            exam_collection.update_many(query, {'$pull': {'subjects': {'subject_code': subject_code}}})


            # Call save_subjects_to_sem_exam to update all matching exams
            save_subjects_to_sem_exam(sem_data['_id'])

        return JsonResponse({'message': 'Subject deleted successfully'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_exam_subjects(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method is allowed'}, status=405)

    exam_id = request.GET.get('exam_id')
    if not exam_id:
        return JsonResponse({'error': 'Missing exam_id in query parameters'}, status=400)

    try:
        exam = exam_collection.find_one({'_id': ObjectId(exam_id)}, {'subjects': 1})
        if not exam:
            return JsonResponse({'error': 'Exam not found'}, status=404)

        subjects = exam.get('subjects', [])
        return JsonResponse({'exam_id':str(exam_id),'subjects': subjects}, status=200, safe=False)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def get_all_exams(request):
    """
    Fetches all exams from 'exam_details' collection and returns simplified data,
    excluding raw binary content to avoid serialization issues.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)


    try:
        exam_collection = db["exam_details"]
        exams_cursor = exam_collection.find({})

        exams = []
        for exam in exams_cursor:
            subjects_cleaned = []
            for subject in exam.get("subjects", []):
                subjects_cleaned.append({
                    "subject_name": subject.get("subject_name", ""),
                    "subject_code": subject.get("subject_code", ""),
                    "question_paper": {
                        "filename": subject.get("question_paper", {}).get("filename", "")
                    },
                    "answer_key": {
                        "filename": subject.get("answer_key", {}).get("filename", "")
                    }
                })

            exams.append({
                "_id": str(exam["_id"]),
                "exam_type": exam.get("exam_type", ""),
                "college": exam.get("college", ""),
                "department": exam.get("department", ""),
                "year": exam.get("year", ""),
                "semester": exam.get("semester", ""),
                "section": exam.get("section", ""),
                "created_by": exam.get("created_by", {}),
                "created_at": exam.get("created_at", ""),
                "subjects": subjects_cleaned,
            })

        return JsonResponse({"exams": exams}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def get_exam_detail_by_id(request, exam_id):
    """
    Returns exam details: metadata, subject list, and answer sheets.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        exam = db["exam_details"].find_one({"_id": ObjectId(exam_id)})

        if not exam:
            return JsonResponse({"error": "Exam not found"}, status=404)

        # Cleaned subject list (only name & code)
        subjects_cleaned = []
        for subject in exam.get("subjects", []):
            subjects_cleaned.append({
                "subject_name": subject.get("subject_name", ""),
                "subject_code": subject.get("subject_code", "")
            })
            
        # Cleaned answer sheets list
        answer_sheets_cleaned = []
        for sheet in exam.get("answer_sheets", []):
            answer_sheets_cleaned.append({
                "answer_sheet_id": sheet.get("answer_sheet_id", ""),
                "student_id": sheet.get("student_id", ""),
                "student_name": sheet.get("student_name", ""),
                "subject_code": sheet.get("subject_code", ""),
                "submitted_at": str(sheet.get("submitted_at", ""))
            })

        # Final response
        exam_data = {
            "_id": str(exam["_id"]),
            "exam_type": exam.get("exam_type", ""),
            "college": exam.get("college", ""),
            "department": exam.get("department", ""),
            "year": exam.get("year", ""),
            "semester": exam.get("semester", ""),
            "section": exam.get("section", ""),
            "created_by": {
                "name": exam.get("created_by", {}).get("name", ""),
                "id": str(exam.get("created_by", {}).get("id", ""))
            },
            "created_at": str(exam.get("created_at", "")),
            "subjects": subjects_cleaned,
            "answer_sheets": answer_sheets_cleaned,  # Add answer sheets to the response
            "batch": exam.get("batch", ""),  # Include batch if available
        }

        return JsonResponse({"exam": exam_data}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

#======================================================= EXAM MANAGEMENT ===========================================================================
@csrf_exempt
def delete_exam(request, exam_id):
    """
    Deletes an exam by ID.
    Requires authentication with JWT token.
    """
    if request.method != "DELETE":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Token Verification
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return JsonResponse({'error': 'Token has expired'}, status=401)
        except jwt.InvalidTokenError:
            return JsonResponse({'error': 'Invalid token'}, status=401)
        
        # Convert string ID to ObjectId
        try:
            exam_object_id = ObjectId(exam_id)
        except:
            return JsonResponse({"error": "Invalid exam ID"}, status=400)
        
        # Find and delete the exam
        exam_collection = db['exam_details']
        result = exam_collection.delete_one({"_id": exam_object_id})
        
        if result.deleted_count == 0:
            return JsonResponse({"error": "Exam not found"}, status=404)
            
        return JsonResponse({"message": "Exam deleted successfully"}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def delete_subject_from_exam(request, exam_id, subject_code):
    """
    Removes a subject from an exam.
    Requires authentication with JWT token.
    """
    if request.method != "DELETE":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Token Verification
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return JsonResponse({'error': 'Token has expired'}, status=401)
        except jwt.InvalidTokenError:
            return JsonResponse({'error': 'Invalid token'}, status=401)
        
        # Convert string ID to ObjectId
        try:
            exam_object_id = ObjectId(exam_id)
        except:
            return JsonResponse({"error": "Invalid exam ID"}, status=400)
        
        # Find the exam
        exam_collection = db['exam_details']
        exam = exam_collection.find_one({"_id": exam_object_id})
        
        if not exam:
            return JsonResponse({"error": "Exam not found"}, status=404)
        
        # Check if subject exists in the exam
        subjects = exam.get("subjects", [])
        subject_exists = any(subject.get("subject_code") == subject_code for subject in subjects)
        
        if not subject_exists:
            return JsonResponse({"error": f"Subject with code {subject_code} not found in this exam"}, status=404)
        
        # Remove the subject from the exam
        result = exam_collection.update_one(
            {"_id": exam_object_id},
            {"$pull": {"subjects": {"subject_code": subject_code}}}
        )
        
        # If this was the last subject, also remove any rubrics associated with it
        if result.modified_count > 0:
            remaining_subjects = exam_collection.find_one({"_id": exam_object_id}).get("subjects", [])
            if not remaining_subjects:
                exam_collection.update_one(
                    {"_id": exam_object_id},
                    {"$set": {"rubrics": []}}
                )
            
        return JsonResponse({"message": f"Subject {subject_code} removed from exam"}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def cleanup_incomplete_exams(request):
    """
    Automatically cleans up exams with incomplete details.
    Criteria for incomplete: missing subjects, missing required fields, etc.
    Can be triggered manually or set up as a scheduled task.
    Requires authentication with JWT token.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Token Verification
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return JsonResponse({'error': 'Token has expired'}, status=401)
        except jwt.InvalidTokenError:
            return JsonResponse({'error': 'Invalid token'}, status=401)
        
        exam_collection = db['exam_details']
        
        # Define criteria for incomplete exams
        incomplete_criteria = {
            "$or": [
                {"subjects": {"$exists": False}},
                {"subjects": {"$size": 0}},
                {"exam_type": {"$exists": False}},
                {"college": {"$exists": False}},
                {"department": {"$exists": False}},
                {"year": {"$exists": False}},
                {"semester": {"$exists": False}}
            ]
        }
        
        # Find and count incomplete exams
        incomplete_exams = exam_collection.find(incomplete_criteria)
        incomplete_count = exam_collection.count_documents(incomplete_criteria)
        
        # Delete incomplete exams
        result = exam_collection.delete_many(incomplete_criteria)
        
        return JsonResponse({
            "message": "Cleanup completed",
            "exams_removed": result.deleted_count,
            "details": "Removed exams with missing subjects or required fields"
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def update_exam_mappings(request, exam_id, subject_code):
    """
    Re-parses question and answer PDFs for a specific exam subject to update question mappings.
    Requires authentication with JWT token.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Token Verification
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return JsonResponse({'error': 'Token has expired'}, status=401)
        except jwt.InvalidTokenError:
            return JsonResponse({'error': 'Invalid token'}, status=401)
        
        # Convert to ObjectId for database queries but use plain string for storage
        try:
            exam_object_id = ObjectId(exam_id)
            exam_id_str = exam_id  # Use the original string directly
        except:
            return JsonResponse({"error": "Invalid exam ID"}, status=400)
        
        # Find the exam
        exam_collection = db['exam_details']
        exam = exam_collection.find_one({"_id": exam_object_id})
        
        if not exam:
            return JsonResponse({"error": "Exam not found"}, status=404)
        
        # Find subject in exam
        subject = None
        for subj in exam.get("subjects", []):
            if subj.get("subject_code") == subject_code:
                subject = subj
                break
                
        if not subject:
            return JsonResponse({"error": f"Subject with code {subject_code} not found in this exam"}, status=404)
        
        # Get the PDFs
        question_data = subject.get("question_paper", {}).get("content")
        answer_data = subject.get("answer_key", {}).get("content")
        
        if not question_data or not answer_data:
            return JsonResponse({"error": "Question paper or answer key missing"}, status=404)
        
        # Write PDFs to temp files
        temp_dir = tempfile.gettempdir()
        question_pdf_path = os.path.join(temp_dir, f"{exam_id}_{subject_code}_question.pdf")
        answer_pdf_path = os.path.join(temp_dir, f"{exam_id}_{subject_code}_answer.pdf")

        with open(question_pdf_path, "wb") as qf:
            qf.write(question_data)

        with open(answer_pdf_path, "wb") as af:
            af.write(answer_data)

        # Parse PDFs with improved parser
        parsed_questions = parse_pdfs(question_pdf_path, answer_pdf_path)
        
        # Update or insert into exam_mapped_questions collection
        # Using plain string exam_id without ObjectId
        db["exam_mapped_questions"].update_one(
            {
                "exam_id": exam_id_str,  # Use plain string
                "subject_code": subject_code
            },
            {
                "$set": {
                    "questions": parsed_questions,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )
        
        # Clean up temp files
        try:
            os.remove(question_pdf_path)
            os.remove(answer_pdf_path)
        except:
            pass
            
        return JsonResponse({
            "message": "Question mappings updated successfully",
            "questions_mapped": len(parsed_questions)
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def get_exam_questions(request, exam_id, subject_code):
    """
    Get all mapped questions for a specific exam subject.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Convert to ObjectId
        try:
            exam_object_id = str(exam_id)
        except:
            return JsonResponse({"error": "Invalid exam ID"}, status=400)
            
        # Find the mapped questions
        question_mapping = db["exam_mapped_questions"].find_one({
            "exam_id": str(exam_object_id), # Convert ObjectId to stringexam_object_id,
            "subject_code": subject_code
        })
        
        if not question_mapping:
            return JsonResponse({"error": "No mapped questions found for this exam subject"}, status=404)
            
        # Convert ObjectId to string for serialization
        question_mapping["_id"] = str(question_mapping["_id"])
        question_mapping["exam_id"] = str(question_mapping["exam_id"])
        
        # Return the mapped questions
        return JsonResponse(question_mapping, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def save_formatted_exam_questions(request):
    """
    Save exam questions with specific formatting:
    1. Clean exam_id to simple string (without ObjectID wrapper)
    2. Preserve original marks for each question (no hardcoding)
    3. Include exam name in the formatted data
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        data = json.loads(request.body)
        
        # Extract the exam_id as a clean string (without ObjectID wrapper)
        raw_exam_id = data.get("exam_id", {}).get("$oid") or data.get("exam_id")
        if not raw_exam_id:
            return JsonResponse({"error": "Invalid exam_id format"}, status=400)
        
        # Extract the subject code and exam name
        subject_code = data.get("subject_code")
        exam_name = data.get("exam_name")
        
        if not subject_code:
            return JsonResponse({"error": "Subject code is required"}, status=400)
        
        # Get the questions - preserve original marks
        questions = data.get("questions", [])
        
        # Create a new document with the formatted data
        formatted_data = {
            "exam_id": str(raw_exam_id),  # Ensure it's a string
            "subject_code": subject_code,
            "exam_name": exam_name,  # Include the exam name
            "questions": questions,
            "created_at": datetime.now()
        }
        
        # Insert the formatted data
        result = db["formatted_exam_questions"].insert_one(formatted_data)
        
        return JsonResponse({
            "message": "Formatted exam questions saved successfully",
            "formatted_id": str(result.inserted_id)
        }, status=201)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def call_gemini_api(student_answer, expected_answer, keywords, max_marks, rubric_items, question_text=""):
    """
    Evaluate a student's answer using Gemini API with topic detection
    to prevent mismatched evaluations.
    """
    # Debug info
    print(f"Calling Gemini API with answer of length {len(student_answer)}")
    print(f"Expected answer of length {len(expected_answer)}")
    print(f"Keywords: {keywords}")
    print(f"Max marks: {max_marks}")
    print(f"Rubric items: {rubric_items}")
    
    # Detect if we have a topic mismatch between student answer and expected answer
    topic_mismatch_check = f"""
Examine these two texts and determine if they are about COMPLETELY DIFFERENT topics:

TEXT 1 (STUDENT ANSWER): 
{student_answer[:300]}...

TEXT 2 (EXPECTED ANSWER):
{expected_answer[:300]}...

Return ONLY a JSON object with this format: {{"is_different_topic": true/false, "student_topic": "brief description", "expected_topic": "brief description"}}
"""
    
    try:
        # First check if topics match
        mismatch_response = model.generate_content(topic_mismatch_check)
        mismatch_output = mismatch_response.text.strip()
        
        # Remove any markdown formatting
        if mismatch_output.startswith("```"):
            mismatch_output = mismatch_output.strip("`").strip()
            if mismatch_output.lower().startswith("json"):
                mismatch_output = mismatch_output[4:].strip()
                
        mismatch_data = json.loads(mismatch_output)

        # Generate rubric guidelines string
        rubric_guidelines = "\n".join([f"- {item['description']} ({item['marks']} marks)" for item in rubric_items])
        print("Rubric guidelines for Gemini:", rubric_guidelines)
        
        # If topics are different, use a special prompt that evaluates on general quality
        if mismatch_data.get("is_different_topic", False):
            print(f"TOPIC MISMATCH DETECTED: Student wrote about {mismatch_data.get('student_topic')} but expected {mismatch_data.get('expected_topic')}")
            
            # Use a prompt that evaluates the answer on its own merits
            prompt = f"""
You are evaluating a student's answer that appears to be on a different topic than expected.
The student was likely answering a different question than what's in our reference.

Student Answer (about {mismatch_data.get('student_topic', 'unknown topic')}):
{student_answer}

Instead of comparing to the expected answer, evaluate this response on its own merits:
1. Is it a well-structured, comprehensive answer about {mismatch_data.get('student_topic', 'its topic')}?
2. Does it demonstrate understanding of concepts, proper terminology and methodology?
3. Is it organized with clear steps/points?
4. Does it include relevant examples and applications?

Rubric to follow (max {max_marks} marks):
{rubric_guidelines} is the guideline for the evaluation so use this mark criteria for the reduction_reasons.
So i need the evaluation that need to be done with the {rubric_guidelines} it act as the key for giving marks deduction of marks.
For example:

If max_marks is 5, and you deduct 3.5 marks in reduction_reasons, then the total marks awarded in rubrick_marks should be 1.5.
Make sure all deductions and awarded marks are clearly tied to specific rubric items from {rubric_guidelines}.

⚠️ Do not include any deductions or evaluations that are not based on the rubric guidelines.

This ensures that:

1.The marks field reflects the accurate total.
2.The rubrick_marks per guideline adds up correctly.
3.The reduction_reasons match the guidelines and don’t exceed max_marks.

Return ONLY a JSON object in this format:
{{"marks": <integer marks awarded>,
 "reason": "<short explanation why you gave these marks>",
 "ai_recommendation": "<Short 3-line suggestion for improvement or next steps from AI>",
 "feedback":"<Start each point with "Student needs to" 1-2 lines of improvement advice>,
 "reduction_reasons": <List each reason for marks reduction clearly as bullet points in 4 to 5 words only with (which guidelines it was based on), along with how many marks were deducted in parentheses. Be specific and concise (e.g., "Missing justification or example (-0.5 marks) Based on the rubricks guidelines only").If no marks were deducted, state "No marks deducted".
 "rubrick_marks" : I need the marks that was given based on the rubrics items only which should be like "<The rubricks guideline> : <Mark> Given for that." give me it in this format type.
 
 So here in the final the {max_marks} is the maximum marks that can be awarded for this question.So the reduction_reasons mark and the rubrick_marks shold be match with the maximum mark equally.Not very high or not very less of the max mark.
 }}
"""
        else:
            # Standard evaluation prompt when topics match
            prompt = f"""
Evaluate this student answer against the expected answer:

Student Answer:
{student_answer}

Expected Answer:
{expected_answer}

Keywords to cover: {', '.join(keywords)}

Max Marks: {max_marks}

Evaluation guidelines:
- Award marks based on completeness, relevance, keyword coverage, clarity, and depth.
- Justify your evaluation → explain what was covered and what was missing.
- Output ONLY a JSON object in the following format:

Rubric to follow (max {max_marks} marks):
{rubric_guidelines} is the guideline for the evaluation so use this mark criteria for the reduction_reasons.
So i need the evaluation that need to be done with the {rubric_guidelines} it act as the key for giving marks and deduction of marks.
For example:

If max_marks is 5, and you deduct 3.5 marks in reduction_reasons, then the total marks awarded in rubrick_marks should be 1.5.
Make sure all deductions and awarded marks are clearly tied to specific rubric items from {rubric_guidelines}.

⚠️ Do not include any deductions or evaluations that are not based on the rubric guidelines.

This ensures that:

1.The marks field reflects the accurate total.
2.The rubrick_marks per guideline adds up correctly.
3.The reduction_reasons match the guidelines and don’t exceed max_marks.

Return ONLY a JSON object in this format:
{{"marks": <integer marks awarded>,
 "reason": "<short explanation why you gave these marks>",
 "ai_recommendation": "<Short 3-line suggestion for improvement or next steps from AI>",
 "feedback":"<Start each point with "Student needs to" 1-2 lines of improvement advice>,
 "reduction_reasons": <List each reason for marks reduction clearly as bullet points in 4 to 5 words only with (which guidelines it was based on), along with how many marks were deducted in parentheses. Be specific and concise (e.g., "Missing justification or example (-0.5 marks) Based on the rubricks guidelines only").If no marks were deducted, state "No marks deducted".
 "rubrick_marks" : I need the marks that was given based on the rubrics items only which should be like "<The rubricks guideline> : <Mark> Given for that." give me it in this format type.
 
 So here in the final the {max_marks} is the maximum marks that can be awarded for this question.So the reduction_reasons mark and the rubrick_marks shold be match with the maximum mark equally.Not very high or not very less of the max mark.
 }}
"""

        # Get the evaluation
        response = model2.generate_content(prompt)
        output = response.text.strip()

        # Clean up the response
        if output.startswith("```"):
            output = output.strip("`").strip()
            if output.lower().startswith("json"):
                output = output[4:].strip()

        result_json = json.loads(output)
        
        score = int(result_json.get("marks", 0))
        reason = result_json.get("reason", "No explanation provided.")
        feedback = result_json.get("feedback", "No feedback provided.")
        ai_recommendation = result_json.get("ai_recommendation", "No recommendation provided.")

        score = min(score, max_marks)
        reduction_reasons_list = result_json.get("reduction_reasons", [])
        reduction_reasons = "\n".join(f"- {r}" for r in reduction_reasons_list)
        rubric_guidelines=rubric_items
        # Extract and normalize rubric marks
        rubrick_marks_raw = result_json.get("rubrick_marks", {})
        # Convert to array format (safe for dict, string, list)
        rubrick_marks = []

        if isinstance(rubrick_marks_raw, dict):
            for item, mark in rubrick_marks_raw.items():
                rubrick_marks.append({
                    "item": str(item).strip(),
                    "mark": f"{mark} marks" if isinstance(mark, (int, float)) else str(mark).strip()
                })

        elif isinstance(rubrick_marks_raw, str):
            entries = rubrick_marks_raw.split(";")
            for entry in entries:
                if ":" in entry:
                    key, val = entry.split(":", 1)
                    mark = val.strip()
                    if not mark.endswith("marks"):
                        mark += " marks"
                    rubrick_marks.append({
                        "item": key.strip(),
                        "mark": mark
                    })

        elif isinstance(rubrick_marks_raw, list):
            if all(isinstance(item, dict) and "item" in item and "mark" in item for item in rubrick_marks_raw):
                rubrick_marks = rubrick_marks_raw
            else:
                for entry in rubrick_marks_raw:
                    if isinstance(entry, str) and ":" in entry:
                        key, val = entry.split(":", 1)
                        mark = val.strip()
                        if not mark.endswith("marks"):
                            mark += " marks"
                        rubrick_marks.append({
                            "item": key.strip(),
                            "mark": mark
                        })

        else:
            rubrick_marks = [{"item": "Unknown", "mark": "0 marks"}]

        print("Rubric marks (normalized):", rubrick_marks)

        # Add context about topic mismatch to the justification if applicable
        justification = f"Gemini AI: {reason}"
        if mismatch_data.get("is_different_topic", False):
            justification = f"TOPIC MISMATCH DETECTED - Student wrote about: {mismatch_data.get('student_topic')}. {justification}"

        return score, justification, feedback, reduction_reasons, ai_recommendation, rubric_guidelines, rubrick_marks

    except Exception as e:
        # If anything fails, return basic info about the error
        error_msg = f"Gemini parsing error: {str(e)} | raw response: {output if 'output' in locals() else 'no output'}"
        print(error_msg)
        raise Exception(error_msg)
    
    
def keyword_based_scoring(student_answer, keywords, max_marks,rubric_items):

    # Generate rubric guidelines string
    rubric_guidelines = "\n".join([f"- {item['description']} ({item['marks']} marks)" for item in rubric_items])
    print("Rubric guidelines for keyword:", rubric_guidelines)

    # print(f"Keyword scoring - comparing answer of length {len(student_answer)} against {len(keywords)} keywords")
    # matched = sum(1 for kw in keywords if kw.lower() in student_answer.lower())
    # total = len(keywords)
    # score = (matched / total) * max_marks if total > 0 else 0
    # justification = f"Keyword-based: Matched {matched}/{total} keywords → awarded {round(score)} marks."
    
    # Build prompt for Gemini feedback generation
    prompt = f"""
You are an evaluator assessing a student's answer based on keyword relevance and overall content quality.

Student Answer:
\"\"\"{student_answer}\"\"\"

Evaluation:

I need the reduction reasons for the marks that was deducted based on the rubrics items only not any random ai evaluations.
Use that guidelines for assigning marks and reduction of marks.
- Rubric items: {rubric_guidelines}
- The maximum marks for this question is {max_marks}.
- Keywords to cover: {', '.join(keywords)}


Now do the following:
1. Create a section titled "reduction_reasons". List each reason for marks reduction clearly as bullet points, along with how many marks were deducted in parentheses. Be specific and concise (e.g., "Missing justification or example (-2 marks)").If no marks were deducted, state "No marks deducted".
make sure that the marks which was deducted was not exceeding the total marks.The reduction resons should be based on the rubric items only.
2. Create a section titled "feedback". Give improvement suggestions in bullet points. Start each point with "Student needs to...". These should be constructive tips, not just restatements of what's missing.
3. Create a section titled "marks". Assign a score based on the rubrick item data only. The score should be an integer between 0 and {max_marks}.

So here in the final the {max_marks} is the maximum marks that can be awarded for this question.So the reduction_reasons mark and the rubrick_marks should be match equally with the maximum mark equally.Not very high or not very less of the max mark.

For example:

If max_marks is 5, and you deduct 3.5 marks in reduction_reasons, then the total marks awarded in rubrick_marks should be 1.5.
Make sure all deductions and awarded marks are clearly tied to specific rubric items from {rubric_guidelines}.

⚠️ Do not include any deductions or evaluations that are not based on the rubric guidelines.

This ensures that:

1.The marks field reflects the accurate total.
2.The rubrick_marks per guideline adds up correctly.
3.The reduction_reasons match the guidelines and don’t exceed max_marks.

Return ONLY a JSON object in this format:

{{
  reduction_reasons: {
    "<reason 1> (-X marks) (which guidelines it was based on)",
    "<reason 2> (-Y marks) (which guidelines it was based on)"
  },
  feedback: {
     "<1-2 lines only of improvement advice in short paragraph format>",
  },
    marks: {
    "<integer marks awarded,The rubricks items has 0.5,1.5 values so you can also award that point mark also for the balance marks in the max marks>"
  }
   reason: {
   "<short explanation why you gave these marks>"
  },
    ai_recommendation: {
    "<Short 3-line suggestion for improvement or next steps from AI>"
  },
    rubrick_marks: {
    "<The rubricks guideline> : <Mark> Given for that."
  }
}}
"""

    try:
        response = model.generate_content(prompt)
        output = response.text.strip()

        # Clean the output (handle if wrapped in ```json or ``` blocks)
        if output.startswith("```"):
            output = output.strip("`").strip()
            if output.lower().startswith("json"):
                output = output[4:].strip()

        # Parse the JSON output
        result_json = json.loads(output)

        # Extract values safely
        reduction_reasons_list = result_json.get("reduction_reasons", [])
        reduction_reasons = "\n".join(f"- {r}" for r in reduction_reasons_list)
        feedback = result_json.get("feedback", [])
        # feedback = "\n".join(f"- {f}" for f in feedback_list)
        marks = result_json.get("marks", 0)  # ✅ Correct
        reason = result_json.get("reason", "No explanation provided.")  # ✅ Correct
        justification = f"Gemini AI: {reason}"
        ai_recommendation = result_json.get("ai_recommendation", "No recommendation provided.")
        rubric_guidelines=rubric_items
        # Extract and normalize rubric marks
        rubrick_marks_raw = result_json.get("rubrick_marks", {})
        # Convert to array format (safe for dict, string, list)
        rubrick_marks = []

        if isinstance(rubrick_marks_raw, dict):
            for item, mark in rubrick_marks_raw.items():
                rubrick_marks.append({
                    "item": str(item).strip(),
                    "mark": f"{mark} marks" if isinstance(mark, (int, float)) else str(mark).strip()
                })

        elif isinstance(rubrick_marks_raw, str):
            entries = rubrick_marks_raw.split(";")
            for entry in entries:
                if ":" in entry:
                    key, val = entry.split(":", 1)
                    mark = val.strip()
                    if not mark.endswith("marks"):
                        mark += " marks"
                    rubrick_marks.append({
                        "item": key.strip(),
                        "mark": mark
                    })

        elif isinstance(rubrick_marks_raw, list):
            if all(isinstance(item, dict) and "item" in item and "mark" in item for item in rubrick_marks_raw):
                rubrick_marks = rubrick_marks_raw
            else:
                for entry in rubrick_marks_raw:
                    if isinstance(entry, str) and ":" in entry:
                        key, val = entry.split(":", 1)
                        mark = val.strip()
                        if not mark.endswith("marks"):
                            mark += " marks"
                        rubrick_marks.append({
                            "item": key.strip(),
                            "mark": mark
                        })

        else:
            rubrick_marks = [{"item": "Unknown", "mark": "0 marks"}]


    except Exception as e:
        error_msg = f"Gemini parsing error: {str(e)} | raw response: {output if 'output' in locals() else 'no output'}"
        print(error_msg)
        raise Exception(error_msg)

    # Final return in your expected structure
    return marks, justification, feedback, reduction_reasons, ai_recommendation, rubric_guidelines , rubrick_marks


@csrf_exempt
def submit_answers(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get("exam_id")
        register_number = data.get("register_number")
        answers = data.get("answers", [])

        if not (exam_id and register_number and answers):
            return JsonResponse({"error": "Missing fields"}, status=400)

        # Fetch exam details to get exam_type
        exam_doc = exam_collection.find_one({"_id": ObjectId(exam_id)})
        if not exam_doc:
            return JsonResponse({"error": "Exam not found in exam_collection"}, status=404)
        exam_type = exam_doc.get("exam_type", "")

        # Find exam mapping with the correct questions and answers
        exam_mapped_doc = exam_mapped_questions_collection.find_one({"exam_id": exam_id})
        if not exam_mapped_doc:
            try:
                exam_mapped_doc = exam_mapped_questions_collection.find_one({"exam_id": str(exam_id)})
            except:
                pass
                
        if not exam_mapped_doc:
            return JsonResponse({"error": "Exam not found in mapped questions"}, status=404)

        student = student_collection.find_one({"register_number": register_number})
        if not student:
            return JsonResponse({"error": "Student not found"}, status=404)

        evaluated_answers = []
        total_marks = 0
        mapped_questions = exam_mapped_doc["questions"]
        
        # Pre-process answers to improve matching
        processed_answers = []
        for a in answers:
            q_no = a.get("question_no", "").strip()
            answer_text = a.get("answer_text", "")
            
            # Check if answer text starts with "a)" or "b)" and question is just a number
            if q_no.isdigit() and answer_text:
                part_match = re.match(r'^\s*([a-zA-Z]\))\s*(.*)', answer_text[:20].lower())
                if part_match:
                    part = part_match.group(1)
                    new_q_no = f"{q_no}{part}"
                    processed_answers.append({
                        "question_no": new_q_no,
                        "answer_text": answer_text
                    })
                    print(f"Reformatted question {q_no} to {new_q_no} based on answer text")
                    continue
            
            processed_answers.append(a)
        
        if processed_answers:
            answers = processed_answers

        for q in mapped_questions:
            q_no = q["question_no"]
            expected_answer = q.get("answer_text", "")
            keywords = q.get("keywords", [])
            max_marks = q.get("marks", 0)
            co = q.get("CO", "")  # Fetch the CO for this question

            print(f"Looking for a match for question {q_no}, keywords: {keywords[:3]}")
            
            student_answer_obj = None
            for a in answers:
                a_no = a.get("question_no", "").strip()
                q_no_clean = q_no.strip()
                answer_text = a.get("answer_text", "")[:100].lower() if a.get("answer_text") else ""
                
                if a_no == q_no_clean:
                    student_answer_obj = a
                    print(f"Found exact match for question {q_no}")
                    break
                
                if a_no.replace(')', '').replace('.', '') == q_no_clean.replace(')', '').replace('.', ''):
                    student_answer_obj = a
                    print(f"Found match after removing parentheses for question {q_no}")
                    break
                
                part_match = re.match(r'(\d+)([a-zA-Z]\)?)', q_no_clean)
                if part_match:
                    q_main = part_match.group(1)
                    q_part = part_match.group(2).lower()
                    if a_no == q_main:
                        if q_part in answer_text:
                            student_answer_obj = a
                            print(f"Found match for question {q_no}: main number match + part in text")
                            break
                
                student_part_match = re.match(r'(\d+)([a-zA-Z]\)?)', a_no)
                if student_part_match:
                    s_main = student_part_match.group(1)
                    s_part = student_part_match.group(2).lower()
                    if q_no_clean.startswith(s_main) and s_part in q_no_clean.lower():
                        student_answer_obj = a
                        print(f"Found match for question {q_no}: student uses part notation")
                        break

            if student_answer_obj:
                student_answer = student_answer_obj.get("answer_text", "")
                print(f"Found student answer for {q_no}, length: {len(student_answer)}")
                
                question_text = q.get("question_text", "")
                
                if max_marks >= 13:
                    try:
                        marks_awarded, justification, feedback, reduction_reasons = call_gemini_api(
                            student_answer, 
                            expected_answer, 
                            keywords, 
                            max_marks, 
                            question_text=question_text
                        )
                        method_used = "ai_evaluation"
                    except Exception as e:
                        print(f"AI evaluation failed: {str(e)}, using keyword fallback")
                        marks_awarded, justification, feedback, reduction_reasons = keyword_based_scoring(student_answer, keywords, max_marks)
                        method_used = "keyword_fallback"
                else:
                    marks_awarded, justification, feedback, reduction_reasons = keyword_based_scoring(student_answer, keywords, max_marks)
                    method_used = "keyword"                
                print(f"Awarded {marks_awarded}/{max_marks} marks, method: {method_used}")
            else:
                marks_awarded = 0
                method_used = "skipped"
                justification = "Student did not answer this question."
                feedback = "No feedback available."
                reduction_reasons = "Student did not answer this question."
                print(f"No student answer found for question {q_no}")

            total_marks += marks_awarded

            evaluated_answers.append({
                "question_no": q_no,
                "marks_awarded": marks_awarded,
                "method_used": method_used,
                "justification": justification,
                "feedback": feedback,
                "reduction_reasons": reduction_reasons,
                "co": co  # Add CO to evaluated answers
            })

        print("\n===== EVALUATION SUMMARY =====")
        print(f"Total marks awarded: {total_marks}")
        print(f"Total questions evaluated: {len(mapped_questions)}")
        print(f"Questions answered: {len([a for a in evaluated_answers if a['marks_awarded'] > 0])}")
        print("")

        student_result_data = {
            "register_number": register_number,
            "name": student.get("name"),
            "evaluated_answers": evaluated_answers,
            "total_marks": total_marks,
            "created_at": datetime.utcnow(),
            "exam_type": exam_type  # Add exam_type to student result
        }

        existing_exam_result = results_collection.find_one({"exam_id": exam_id})

        if existing_exam_result:
            results_collection.update_one(
                {"exam_id": exam_id},
                {"$push": {"results": student_result_data}}
            )
        else:
            results_collection.insert_one({
                "exam_id": exam_id,
                "exam_type": exam_type,  # Add exam_type at the top level
                "results": [student_result_data]
            })

        return JsonResponse({
            "message": "Evaluation completed",
            "result": {
                "exam_id": exam_id,
                "student_result": student_result_data
            }
        }, status=200)

    except Exception as e:
        print(f"Error in submit_answers: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500) 
    
@csrf_exempt
def get_exam_results(request, exam_id):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Find all results for this exam
        results = list(results_collection.find({"exam_id": {"$in": [exam_id, ObjectId(exam_id)]}}))
        
        # If no results found
        if not results:
            # Fetch all answer sheets for this exam regardless of result availability
            answer_sheets = list(answer_sheet_collection.find(
                {"exam_id": {"$in": [exam_id, ObjectId(exam_id)]}},
                {"_id": 1, "student_id": 1, "student_name": 1, "file_name": 1, 
                "submitted_at": 1, "is_evaluated": 1, "total_marks": 1}
            ))
            
            serialized_sheets = []
            for sheet in answer_sheets:
                serialized_sheets.append({
                    "id": str(sheet["_id"]),
                    "student_id": sheet.get("student_id", ""),
                    "student_name": sheet.get("student_name", "Unknown"),
                    "file_name": sheet.get("file_name", ""),
                    "submitted_at": sheet.get("submitted_at").isoformat() if sheet.get("submitted_at") else None,
                    "is_evaluated": sheet.get("is_evaluated", False),
                    "total_marks": sheet.get("total_marks", 0)
                })
            
            # If no results but we have answer sheets
            if serialized_sheets:
                return JsonResponse({
                    "message": "Found answer sheets but no evaluation results yet",
                    "answer_sheets": serialized_sheets
                }, status=200)
            else:
                return JsonResponse({
                    "message": "No results have been published yet. No answer sheets uploaded."
                }, status=200)

        # Process and serialize all results
        serialized_results = []
        for result in results:
            # Serialize top-level ObjectId and datetime
            serialized_result = {
                "_id": str(result["_id"]),
                "exam_id": str(result["exam_id"]),
                "register_number": result.get("register_number", ""),
                "name": result.get("name", ""),
                "evaluated_answers": result.get("evaluated_answers", []),
                "total_marks": result.get("total_marks", 0),
                "created_at": result.get("created_at", "").isoformat() if result.get("created_at") else None,
                "answer_sheet_id": str(result.get("answer_sheet_id", ""))
            }
            serialized_results.append(serialized_result)
            
        # Fetch all answer sheets for this exam
        answer_sheets = list(answer_sheet_collection.find(
            {"exam_id": {"$in": [exam_id, ObjectId(exam_id)]}},
            {"_id": 1, "student_id": 1, "student_name": 1, "file_name": 1, 
            "submitted_at": 1, "is_evaluated": 1, "total_marks": 1}
        ))
        
        serialized_sheets = []
        for sheet in answer_sheets:
            serialized_sheets.append({
                "id": str(sheet["_id"]),
                "student_id": sheet.get("student_id", ""),
                "student_name": sheet.get("student_name", "Unknown"),
                "file_name": sheet.get("file_name", ""),
                "submitted_at": sheet.get("submitted_at").isoformat() if sheet.get("submitted_at") else None,
                "is_evaluated": sheet.get("is_evaluated", False),
                "total_marks": sheet.get("total_marks", 0)
            })

        # Return both evaluation results and all answer sheets
        return JsonResponse({
            "exam_results": serialized_results,
            "answer_sheets": serialized_sheets
        }, status=200)

    except Exception as e:
        print(f"Error in get_exam_results: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)
       
# @csrf_exempt
# def get_student_results(request, register_number):
#     if request.method != "GET":
#         return JsonResponse({"error": "Invalid request method"}, status=405)

#     try:
#         final_output = []

#         # 🔍 Step 1: Get student profile
#         student_info = student_collection.find_one({"register_number": register_number})
#         if not student_info:
#             return JsonResponse({"error": "Student not found"}, status=404)

#         student_data = {
#             "name": student_info.get("name"),
#             "email": student_info.get("email"),
#             "department": student_info.get("department"),
#             "year": student_info.get("year"),
#             "semester": student_info.get("semester"),
#             "section": student_info.get("section"),
#             "register_number": student_info.get("register_number"),
#         }

#         # 🔍 Step 2: Get all results
#         student_results = list(results_collection.find({"register_number": register_number}))

#         for res in student_results:
#             exam_id = res.get("exam_id")
#             exam_info = exam_collection.find_one({"_id": exam_id})

#             # Exam metadata
#             exam_meta = {}
#             if exam_info:
#                 exam_meta = {
#                     "exam_id": str(exam_info.get("_id")),
#                     "exam_type": exam_info.get("exam_type"),
#                     "college": exam_info.get("college"),
#                     "department": exam_info.get("department"),
#                     "year": exam_info.get("year"),
#                     "semester": exam_info.get("semester"),
#                     "section": exam_info.get("section"),
#                 }

#             # Result info
#             student_result = {
#                 "_id": str(res.get("_id")),
#                 "total_marks": res.get("total_marks"),
#                 "evaluated_answers": res.get("evaluated_answers", []),
#                 "created_at": res.get("created_at").isoformat() if res.get("created_at") else None
#             }

#             final_output.append({
#                 "exam_details": exam_meta,
#                 "result": student_result
#             })

#         return JsonResponse({
#             "student": student_data,
#             "results": final_output
#         }, status=200)

#     except Exception as e:
#         return JsonResponse({"error": str(e)}, status=500)

# Function to need to update according to the frontend UI

@csrf_exempt
def get_exam_details_report(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        if request.content_type and "application/json" in request.content_type:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON format"}, status=400)
        else:
            return JsonResponse({"error": "Content-Type must be application/json"}, status=400)

        register_number = data.get("register_number", "").strip()
        if not register_number:
            return JsonResponse({"error": "No register_number provided"}, status=400)

        student_doc = student_collection.find_one({
            "register_number": {"$regex": f"^{re.escape(register_number)}$", "$options": "i"}
        })
        if not student_doc:
            return JsonResponse({"message": f"No student found with register number: {register_number}"}, status=404)

        student_info = {
            "name": student_doc.get("name", ""),
            "email": student_doc.get("email", ""),
            "department": student_doc.get("department", ""),
            "year": student_doc.get("year", ""),
            "section": student_doc.get("section", ""),
            "register_number": student_doc.get("register_number", "")
        }

        matched_result_docs = results_collection.find({
            "results.subjects.students.register_number": {
                "$regex": f"^{re.escape(register_number)}$",
                "$options": "i"
            }
        })

        results_list = []
        exam_ids = set()
        for doc in matched_result_docs:
            exam_id = str(doc.get("exam_id", ""))
            if not exam_id:
                continue
            exam_ids.add(exam_id)
            subjects = doc.get("results", {}).get("subjects", [])

            for subject in subjects:
                for student in subject.get("students", []):
                    if student.get("register_number", "").strip().lower() != register_number.lower():
                        continue

                    created_at_value = student.get("created_at")
                    created_at_iso = created_at_value.isoformat() if isinstance(created_at_value, datetime) else str(created_at_value)

                    results_list.append({
                        "exam_id": exam_id,
                        "exam_type": doc.get("exam_type", ""),
                        "subject_code": subject.get("subject_code", ""),
                        "subject_name": subject.get("subject_name", ""),
                        "total_marks": student.get("total_marks", 0),
                        "answer_sheet_id": student.get("answer_sheet_id", ""),
                        "answer_sheet_file_url": student.get("answer_sheet_file_url", ""),
                        "created_at": created_at_iso,
                        "status": "Passed" if student.get("total_marks", 0) >= 50 else "Failed"
                    })

        exam_details = []
        for exam_id in exam_ids:
            try:
                exam = exam_collection.find_one({"_id": ObjectId(exam_id)})
                if not exam:
                    continue
                subject_with_date = next((sub for sub in exam.get("subjects", []) if sub.get("examDate") and sub.get("session")), None)
                exam_details.append({
                    "exam_id": exam_id,
                    "exam_type": exam.get("exam_type", ""),
                    "semester": exam.get("semester", ""),
                    "examDate": subject_with_date.get("examDate", "") if subject_with_date else "",
                    "session": subject_with_date.get("session", "") if subject_with_date else ""
                })
            except:
                continue

        # Combine details and group by (subject_code, semester)
        subject_map = {}
        for result in results_list:
            exam = next((e for e in exam_details if e["exam_id"] == result["exam_id"]), {})
            semester = exam.get("semester", "")
            subject_key = (result["subject_code"], semester)

            exam_entry = {
                "exam_id": result["exam_id"],
                "exam_type": exam.get("exam_type", result.get("exam_type", "")),
                "semester": semester,
                "examDate": exam.get("examDate", ""),
                "session": exam.get("session", ""),
                "subject_code": result["subject_code"],
                "subject_name": result["subject_name"],
                "attendance_status": "Completed",
                "total_marks": result["total_marks"],
                "answer_sheet_id": result["answer_sheet_id"],
                "answer_sheet_file_url": result["answer_sheet_file_url"],
                "created_at": result["created_at"],
                "status": result["status"]
            }

            if subject_key not in subject_map:
                subject_map[subject_key] = {
                    "subject_code": result["subject_code"],
                    "subject_name": result["subject_name"],
                    "semester": semester,
                    "exams": []
                }
            subject_map[subject_key]["exams"].append(exam_entry)

        grouped_exam_results = list(subject_map.values())

        internals = [r["total_marks"] for sub in grouped_exam_results for r in sub["exams"] if r["exam_type"] in ["IAE - 1", "IAE - 2", "IAE - 3"]]
        semester_exams = [r["total_marks"] for sub in grouped_exam_results for r in sub["exams"] if r["exam_type"] == "Semester"]
        failed_exams = [r for sub in grouped_exam_results for r in sub["exams"] if r["status"] == "Failed" and r["exam_type"] == "Semester"]

        internals_average = sum(internals) / len(internals) if internals else 0
        semester_average = sum(semester_exams) / len(semester_exams) if semester_exams else 0

        def marks_to_grade_points(marks):
            if marks >= 90: return 10
            elif marks >= 80: return 9
            elif marks >= 70: return 8
            elif marks >= 60: return 7
            elif marks >= 50: return 6
            return 0

        semester_grade_points = [marks_to_grade_points(marks) for marks in semester_exams]
        semester_cgpa = sum(semester_grade_points) / len(semester_grade_points) if semester_grade_points else 0

        stats = {
            "internals_average": round(internals_average, 2),
            "semester_cgpa": round(semester_cgpa, 2),
            "semester_average": round(semester_average, 2),
            "number_of_arrears": len(failed_exams)
        }

        return JsonResponse({
            "student": student_info,
            "exam_results": grouped_exam_results,
            "stats": stats
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def get_student_results(request, register_number):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Normalize register number
        target_reg_num = register_number.strip().lower()

        # Fetch student details
        student_doc = student_collection.find_one({"register_number": register_number})
        if not student_doc:
            return JsonResponse({"message": f"No student found with register number: {register_number}"}, status=404)

        student_info = {
            "name": student_doc.get("name", ""),
            "email": student_doc.get("email", ""),
            "department": student_doc.get("department", ""),
            "year": student_doc.get("year", ""),
            "semester": student_doc.get("semester", None),
            "section": student_doc.get("section", ""),
            "register_number": student_doc.get("register_number", "")
        }

        # Fetch relevant exams
        exams = list(exam_collection.find({
            "department": student_info["department"],
            "year": student_info["year"],
            "semester": student_info["semester"]
        }))

        # Fetch only result documents where student is present
        matched_result_docs = results_collection.find({
            "results.subjects.students.register_number": register_number
        })

        # Extract results
        results_list = []
        for doc in matched_result_docs:
            exam_id = str(doc.get("_id"))
            subjects = doc.get("results", {}).get("subjects", [])

            for subject in subjects:
                for student in subject.get("students", []):
                    if student.get("register_number", "").strip().lower() == target_reg_num:
                        created_at_value = student.get("created_at")
                        if isinstance(created_at_value, datetime):
                            created_at_iso = created_at_value.isoformat()
                        elif isinstance(created_at_value, str):
                            created_at_iso = created_at_value
                        else:
                            created_at_iso = None

                        results_list.append({
                            "name": student.get("name", ""),
                            "evaluated_answers": student.get("evaluated_answers", []),
                            "total_marks": student.get("total_marks", 0),
                            "created_at": created_at_iso,
                            "answer_sheet_id": exam_id,
                            "answer_sheet_file_url": student.get("answer_sheet_file_url", ""),
                            "subject_code": subject.get("subject_code", ""),
                            "subject_name": subject.get("subject_name", ""),
                            "register_number": student.get("register_number", "")
                        })

        # Combine exam info with attendance status
        exam_results = []
        for exam in exams:
            exam_id = str(exam.get("_id"))
            exam_name = exam.get("exam_type", "")
            session = "Forenoon" if "forenoon" in exam_name.lower() else "Afternoon"

            # Format date
            created_at = exam.get("created_at")
            formatted_date = ""
            if isinstance(created_at, datetime):
                formatted_date = created_at.strftime("%d-%m-%y")
            elif isinstance(created_at, dict) and "$date" in created_at:
                try:
                    date_obj = datetime.strptime(created_at["$date"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    formatted_date = date_obj.strftime("%d-%m-%y")
                except:
                    formatted_date = created_at["$date"]

            # Check attendance
            attendance_status = "Absent"
            for result in results_list:
                if result.get("answer_sheet_id") == exam_id:
                    attendance_status = "Completed"
                    break

            # Extract first subject (optional - depends on schema)
            first_subject = exam.get("subjects", [{}])[0]
            exam_results.append({
                "exam_name": exam_name,
                "subject_code": first_subject.get("subject_code", ""),
                "subject_name": first_subject.get("subject_name", ""),
                "date": formatted_date,
                "session": session,
                "attendance_status": attendance_status
            })

        return JsonResponse({
            "student": student_info,
            "exams": exam_results,
            "results": results_list
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def student_exam_report(request, exam_id, subject_code, register_number):

    """
    Generates a detailed report for a student's exam performance in a specific subject.

    Args:
        request (HttpRequest): The HTTP request object.
        exam_id (str): The ID of the exam.
        subject_code (str): The code of the subject.
        register_number (str): The student's registration number.

    Returns:
        JsonResponse: A JSON response containing the student's performance breakdown,
                      marks, rank, and Bloom's taxonomy performance data.

    The report includes:
    - A detailed breakdown of each question, including AI-awarded marks, feedback,
      and Bloom's taxonomy level.
    - Total AI and manual marks.
    - The student's rank among peers in the subject.
    - Performance percentages for each Bloom's taxonomy level.
    """

    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        subject_code = subject_code.strip().upper()
        register_number = register_number.strip().upper()

        # Bloom's level mapping
        bloom_level_mapping = {
            "REM": "Remember",
            "UND": "Understand",
            "APP": "Apply",
            "ANA": "Analyze",
            "EVA": "Evaluate",
            "CRE": "Create"
        }

        # Fetch result document
        result_doc = results_collection.find_one({"exam_id": exam_id})
        if not result_doc:
            return JsonResponse({"error": "Exam results not found"}, status=404)

        subjects = result_doc.get("results", {}).get("subjects", [])
        subject_data = next(
            (s for s in subjects if s.get("subject_code", "").upper() == subject_code),
            None
        )
        if not subject_data:
            return JsonResponse({"error": "Subject not found in results"}, status=404)

        student_data = next(
            (stu for stu in subject_data.get("students", [])
             if stu.get("register_number", "").strip().upper() == register_number),
            None
        )
        if not student_data:
            return JsonResponse({"error": "Student not found in subject results"}, status=404)

        evaluated_answers = student_data.get("evaluated_answers", [])
        staff_mark = student_data.get("staff_mark", None)

        # Fetch mapped questions
        mapped_doc = db["exam_mapped_questions"].find_one({
            "exam_id": exam_id,
            "subject_code": subject_code
        })
        if not mapped_doc:
            return JsonResponse({"error": "Mapped questions not found"}, status=404)

        mapped_questions = mapped_doc.get("questions", [])

        # Build a robust question map that supports both plain and part-based question numbers
        question_map = {}
        for q in mapped_questions:
            qno = str(q.get("question_no", "")).strip()
            part = str(q.get("part", "")).strip().upper()

            if part:  # Example: "6b)"
                full_qno = f"{qno}{part})"
                question_map[full_qno] = {
                    "text": q.get("question_text", ""),
                    "marks": q.get("marks", 0)
                }

            # Also store "6" or "1"
            question_map[qno] = {
                "text": q.get("question_text", ""),
                "marks": q.get("marks", 0)
            }

        # Initialize Bloom's performance tracking
        bloom_performance = {
            "Remember": {"awarded": 0, "total": 0},
            "Understand": {"awarded": 0, "total": 0},
            "Apply": {"awarded": 0, "total": 0},
            "Analyze": {"awarded": 0, "total": 0},
            "Evaluate": {"awarded": 0, "total": 0},
            "Create": {"awarded": 0, "total": 0}
        }

        breakdown = []
        ai_total = 0
        total_possible = 0

        for ans in evaluated_answers:
            qno = str(ans.get("question_no", "")).strip()
            justification = ans.get("justification", "")
            reduction_reasons = ans.get("reduction_reasons", "")

            # Skip questions where both justification and reduction_reasons are "Answer not found in extracted content"
            if (
                justification == "Answer not found in extracted content" and
                reduction_reasons == "Answer not found in extracted content"
            ):
                continue

            mapped_info = question_map.get(qno, {"text": "", "marks": 0})
            q_text = mapped_info.get("text", "")
            q_mark = mapped_info.get("marks", 0)
            ai_mark = ans.get("marks_awarded", 0)
            bloom_level = ans.get("bloom_level", "")

            # Map short Bloom's level to full name
            bloom_level_full = bloom_level_mapping.get(bloom_level, "Unknown")

            # Update Bloom's performance
            if bloom_level_full in bloom_performance:
                bloom_performance[bloom_level_full]["awarded"] += ai_mark
                bloom_performance[bloom_level_full]["total"] += q_mark

            ai_total += ai_mark
            total_possible += q_mark

            # Include rubric_items and rubric_marks in the response
            rubric_items = ans.get("rubric_items", [])
            rubric_marks = ans.get("rubric_marks", [])

            breakdown.append({
                "questionNumber": qno,
                "question": q_text,
                "totalMarks": q_mark,
                "aiMarks": ai_mark,
                "justification": justification,
                "feedback": ans.get("feedback") or "",
                "reduction_reasons": reduction_reasons,
                "ai_recommendation": ans.get("ai_recommendation") or "",
                "rubric_items": [
                    {
                        "description": item.get("description", ""),
                        "marks": item.get("marks", 0)
                    } for item in rubric_items
                ],
                "rubric_marks": [
                    {
                        "item": item.get("item", ""),
                        "mark": item.get("mark", "0 marks")
                    } for item in rubric_marks
                ],
                "bloom_level": bloom_level_full
            })

        # Calculate Bloom's performance percentages
        bloom_performance_data = {
            level: (
                (data["awarded"] / data["total"] * 100) if data["total"] > 0 else 0
            ) for level, data in bloom_performance.items()
        }

        # Calculate the rank of the student
        subjects = result_doc.get("results", {}).get("subjects", [])
        for subject in subjects:
            if subject.get("subject_code", "").upper() == subject_code:
                students = subject.get("students", [])
                # Sort students by total marks in descending order
                sorted_students = sorted(students, key=lambda x: x.get("total_marks", 0), reverse=True)
                # Find the rank of the student
                rank = next(
                    (i + 1 for i, stu in enumerate(sorted_students)
                     if stu.get("register_number", "").strip().upper() == register_number),
                    None
                )

        return JsonResponse({
            "questions": breakdown,
            "marks": {
                "manualMarks": staff_mark,
                "ai_total_mark": ai_total,
                "total_mark": total_possible
            },
            "rank": rank,
            "performance": bloom_performance_data
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def upload_answer_sheet(request):
    """Upload an answer sheet for a student."""
    if request.method == "POST":
        try:
            # Extract data from request
            exam_id = request.POST.get("exam_id")
            student_id = request.POST.get("student_id")
            subject_code = request.POST.get("subject_code")
            is_reupload = request.POST.get("is_reupload", "false").lower() == "true"

            # Get the uploaded file
            answer_sheet = request.FILES.get("answer_sheet")

            # Validate required fields
            if not exam_id or not student_id or not answer_sheet:
                return JsonResponse({"error": "Missing required fields"}, status=400)

            # Check if file is PDF
            if not answer_sheet.name.lower().endswith('.pdf'):
                return JsonResponse({"error": "Only PDF files are allowed"}, status=400)

            # Generate filename
            filename = f"answersheets/{exam_id}_{subject_code}_{student_id}_answersheet.pdf"

            # Upload to S3
            file_url = upload_to_s3(answer_sheet, filename)
            if not file_url:
                return JsonResponse({"error": "Failed to upload file to S3"}, status=500)

            # Fetch student name
            student = student_collection.find_one({"register_number": student_id})
            if not student:
                return JsonResponse({"error": "Student not found"}, status=404)
            
            # Check if student is a dictionary
            if not isinstance(student, dict):
                return JsonResponse({"error": "Invalid student data format in database"}, status=500)
            
            student_name = student.get("name")
            if not student_name:
                return JsonResponse({"error": "Student name not found in database"}, status=404)

            # Prepare student data
            student_data = {
                "student_id": student_id,
                "student_name": student_name,
                "subject_code": subject_code,
                "file_url": file_url,
                "submitted_at": datetime.now(),
                "has_answer_sheet": True,
                "is_evaluated": False,
                "status": "submitted"
            }

            # Check if a document with the same exam_id and subject_code exists
            existing_doc = answer_sheet_collection.find_one({
                "exam_id": exam_id,
                "subjects.subject_code": subject_code
            })

            if existing_doc:
                # Validate that subjects is a list
                if not isinstance(existing_doc["subjects"], list):
                    return JsonResponse({"error": "Invalid subjects format in database: expected a list"}, status=500)

                # Document exists, check if the subject already exists
                subject_index = next(
                    (i for i, subj in enumerate(existing_doc["subjects"]) if subj["subject_code"] == subject_code),
                    None
                )
                if subject_index is None:
                    # Subject not found, append new subject with student data
                    answer_sheet_collection.update_one(
                        {
                            "exam_id": exam_id
                        },
                        {
                            "$push": {
                                "subjects": {
                                    "subject_code": subject_code,
                                    "students": [student_data]
                                }
                            }
                        }
                    )
                    message = "Answer sheet uploaded successfully"
                else:
                    # Subject exists, ensure students is a list
                    students = existing_doc["subjects"][subject_index]["students"]
                    if not isinstance(students, list):
                        # Convert students to a list if it's an object
                        students = [students]
                        # Update the document to fix the schema
                        answer_sheet_collection.update_one(
                            {
                                "exam_id": exam_id,
                                "subjects.subject_code": subject_code
                            },
                            {
                                "$set": {
                                    f"subjects.{subject_index}.students": students
                                }
                            }
                        )

                    # Check if the student already has a submission
                    student_index = next(
                        (i for i, stu in enumerate(students) if stu["student_id"] == student_id),
                        None
                    )

                    if student_index is not None and is_reupload:
                        # Reupload: Update the existing student's entry
                        answer_sheet_collection.update_one(
                            {
                                "exam_id": exam_id,
                                "subjects.subject_code": subject_code
                            },
                            {
                                "$set": {
                                    f"subjects.{subject_index}.students.{student_index}": student_data
                                }
                            }
                        )
                        message = "Answer sheet re-uploaded successfully"
                    else:
                        # Append new student to the existing subject's students array
                        answer_sheet_collection.update_one(
                            {
                                "exam_id": exam_id,
                                "subjects.subject_code": subject_code
                            },
                            {
                                "$push": {
                                    f"subjects.{subject_index}.students": student_data
                                }
                            }
                        )
                        message = "Answer sheet uploaded successfully"
            else:
                # No document exists, create a new one
                answer_sheet_doc = {
                    "exam_id": exam_id,
                    "subjects": [
                        {
                            "subject_code": subject_code,
                            "students": [student_data]
                        }
                    ]
                }
                answer_sheet_collection.insert_one(answer_sheet_doc)
                message = "Answer sheet uploaded successfully"

            return JsonResponse({
                "success": True,
                "message": message,
                #"file_url": file_url
            }, status=201)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid request method"}, status=405)

@csrf_exempt
def extract_handwritten_answers(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    if 'file' not in request.FILES:
        return JsonResponse({"error": "Missing file"}, status=400)

    try:
        import tempfile, os, numpy as np, cv2, PIL.Image, io, re
        from pdf2image import convert_from_path
        from google.generativeai import GenerativeModel

        genai.configure(api_key="AIzaSyDFdXTNcjx1bezvBC4-jCE89VUThmF0xhQ")
        model = GenerativeModel("models/gemini-2.0-flash")

        # Save uploaded PDF to temp file
        uploaded_file = request.FILES['file']
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_file:
            for chunk in uploaded_file.chunks():
                tmp_file.write(chunk)
            tmp_pdf_path = tmp_file.name

        # ✅ Step 1: convert PDF → filtered images
        pages = convert_from_path(tmp_pdf_path)
        all_texts = []

        for i, page in enumerate(pages):
            img_np = np.array(page)
            img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

            hsv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2HSV)
            lower_red1 = np.array([0, 100, 100])
            upper_red1 = np.array([10, 255, 255])
            lower_red2 = np.array([160, 100, 100])
            upper_red2 = np.array([180, 255, 255])
            mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
            mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
            red_mask = cv2.bitwise_or(mask1, mask2)
            non_red_mask = cv2.bitwise_not(red_mask)
            filtered_img = cv2.bitwise_and(img_cv, img_cv, mask=non_red_mask)
            filtered_rgb = cv2.cvtColor(filtered_img, cv2.COLOR_BGR2RGB)
            filtered_pil = PIL.Image.fromarray(filtered_rgb)

            # ✅ Convert image to bytes
            buffered = io.BytesIO()
            filtered_pil.save(buffered, format="JPEG")
            img_bytes = buffered.getvalue()

            # ✅ Gemini prompt
            if i + 1 == len(pages):  # last page
                prompt = """
                    This is the last page of an answer sheet containing the main questions 1-5.
                    Extract all the text exactly as written in the image, focusing ONLY on handwritten content in blue or black ink.
                    Preserve the question numbers exactly as they appear.
                    Ignore any text in red ink.
                """
            else:
                prompt = """
                    Extract all the text exactly as written in the image, focusing ONLY on handwritten content in blue or black ink.
                    Preserve question numbers like '6)b)', '7)b)', etc.
                    Ignore any text in red ink.
                    Ignore headers like "PART-A", "PART-B", "PART-C".
                    Keep bullet points, line breaks.
                """

            # ✅ Gemini call
            response = model.generate_content([prompt, PIL.Image.open(io.BytesIO(img_bytes))])
            raw_text = response.text.strip()

            # ✅ Clean basic unwanted content
            cleaned = re.sub(r'\b(PART-[A-C])\b', '', raw_text)
            cleaned = re.sub(r'^\s*(Page\s*)?\d+\s*$', '', cleaned, flags=re.MULTILINE)
            cleaned = re.sub(r'\bSNSCE\b|\bSNS\b', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\n\s*\n', '\n\n', cleaned)

            all_texts.append(cleaned.strip())

        # ✅ Step 2: process extracted text
        main_questions_text = all_texts[-1]
        main_questions = {}
        q_pattern = r'(\d+)\.\s*(.*?)(?=\d+\.|\Z)'
        for match in re.finditer(q_pattern, main_questions_text, re.DOTALL):
            q_num = match.group(1)
            q_text = match.group(2).strip()
            main_questions[q_num] = q_text

        sub_questions = {}
        sub_pattern = r'(\d+)\)?([a-zA-Z]?)\)?(.*?)(?=\n\d+\)?[a-zA-Z]?\)|\n\d+\)|\n\d+\)|\Z)'
        combined_text = "\n".join(all_texts[:-1])
        for match in re.finditer(sub_pattern, combined_text, re.DOTALL):
            q_main = match.group(1)
            q_sub = match.group(2)
            q_text = match.group(3).strip()
            q_id = f"{q_main}){q_sub})"
            sub_questions[q_id] = q_text

        answers = []
        for q_num in sorted(main_questions.keys(), key=int):
            answers.append({"question_no": q_num, "answer_text": main_questions[q_num]})
        for q_id in sorted(sub_questions.keys(), key=lambda x: int(re.match(r'(\d+)', x).group(1))):
            answers.append({"question_no": q_id, "answer_text": sub_questions[q_id]})

        # ✅ Clean up temp file
        os.remove(tmp_pdf_path)

        return JsonResponse({"answers": answers}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
       
@csrf_exempt 
def view_answer_sheet(request, filename):
    """
    Retrieves an answer sheet by filename and returns it as a PDF file.
    
    Args:
        request (HttpRequest): The HTTP request object.
        filename (str): The filename of the answer sheet to retrieve.
        
    Returns:
        HttpResponse: A PDF file response with the answer sheet content.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Find the answer sheet by filename
        answer_sheet = answer_sheet_collection.find_one({"file_name": filename})
        
        if not answer_sheet:
            return JsonResponse({"error": "Answer sheet not found"}, status=404)
        
        # Get the PDF content
        pdf_content = answer_sheet.get("content")
        
        if not pdf_content:
            return JsonResponse({"error": "PDF content is missing"}, status=404)
        
        # Create a HttpResponse object with PDF content
        from django.http import HttpResponse
        response = HttpResponse(pdf_content, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        
        return response
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def list_answer_sheets(request):
    """
    List all answer sheets with their IDs for testing purposes
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Find all answer sheets
        sheets = answer_sheet_collection.find({}, {"_id": 1, "student_id": 1, "exam_id": 1, "file_name": 1})
        
        result = []
        for sheet in sheets:
            result.append({
                "id": str(sheet["_id"]),
                "student_id": sheet.get("student_id", ""),
                "exam_id": str(sheet.get("exam_id", "")),
                "file_name": sheet.get("file_name", "")
            })
        
        return JsonResponse({"answer_sheets": result}, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def process_and_evaluate_answer_sheet(request):
    """
    Processes an uploaded answer sheet using AI, extracts answers,
    and initiates the evaluation process.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        data = json.loads(request.body)
        exam_id = data.get("exam_id")
        register_number = data.get("register_number")
        subject_code = data.get("subject_code")
        answer_sheet_id = data.get("answer_sheet_id")
                
        if not all([exam_id, register_number, subject_code]):
            return JsonResponse({"error": "Missing required fields"}, status=400)
        
        # Debug logging to track execution flow
        print(f"Processing answer sheet for student {register_number}, subject {subject_code}")
        
        # IMPORTANT: Check for mapped questions early to avoid unnecessary processing
        exam_mapped_doc = exam_mapped_questions_collection.find_one({
            "$or": [
                {"exam_id": exam_id, "subject_code": subject_code},
                {"exam_id": str(exam_id), "subject_code": subject_code}
            ]
        })
        
        if not exam_mapped_doc:
            print(f"No mapped questions found for exam_id: {exam_id}, subject_code: {subject_code}")
            return JsonResponse({
                "error": "No exam questions found for evaluation. Please upload question paper and answer key first.",
                "missing_questions": True
            }, status=400)
        
        mapped_questions = exam_mapped_doc.get("questions", [])
        if not mapped_questions:
            print(f"Empty questions array for exam_id: {exam_id}, subject_code: {subject_code}")
            return JsonResponse({
                "error": "No questions found in the mapped data. Please check the question paper and answer key.",
                "missing_questions": True
            }, status=400)
        
        print(f"Found {len(mapped_questions)} mapped questions for evaluation")
        
        # Fetch exam details to get exam_type
        exam_doc = exam_collection.find_one({"_id": ObjectId(exam_id)})
        if not exam_doc:
            return JsonResponse({"error": "Exam not found in exam_collection"}, status=404)
        exam_type = exam_doc.get("exam_type", "")

        # Try to find answer sheet by ID first, then by exam+student+subject
        if answer_sheet_id:
            answer_sheet = answer_sheet_collection.find_one({"_id": ObjectId(answer_sheet_id)})
            if not answer_sheet:
                return JsonResponse({"error": f"Answer sheet not found with ID: {answer_sheet_id}"}, status=404)
        else:
            # Search using multiple possible document structures
            answer_sheet = answer_sheet_collection.find_one({
                "$or": [
                    {"exam_id": exam_id, "student_id": register_number, "subject_code": subject_code},
                    {"exam_id": exam_id, "subjects.subject_code": subject_code, "subjects.students.student_id": register_number}
                ]
            })
            
            if not answer_sheet:
                return JsonResponse({"error": f"Answer sheet not found for student {register_number} in subject {subject_code}"}, status=404)
            
            answer_sheet_id = str(answer_sheet["_id"])
        
        # Locate the file_url in the document (handle both flat and nested structures)
        file_url = None
        if "file_url" in answer_sheet:
            file_url = answer_sheet.get("file_url")
        else:
            for subject in answer_sheet.get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    for student in subject.get("students", []):
                        if student.get("student_id") == register_number:
                            file_url = student.get("file_url")
                            break
                    if file_url:
                        break
        
        if not file_url:
            return JsonResponse({"error": "No file URL found for this answer sheet"}, status=404)
        
        print(f"Found answer sheet with file_url: {file_url}")
        
        # Download the PDF file from S3
        temp_dir = tempfile.gettempdir()
        temp_pdf_path = os.path.join(temp_dir, f"{register_number}_{exam_id}_temp.pdf")
        
        try:
            s3_key = file_url.replace(f"{AWS_S3_CUSTOM_DOMAIN}/", "")
            print(f"Downloading from S3: {s3_key} to {temp_pdf_path}")
            s3_client.download_file(
                AWS_STORAGE_BUCKET_NAME, 
                s3_key, 
                temp_pdf_path
            )
            print(f"PDF successfully downloaded to {temp_pdf_path}")
        except Exception as s3_error:
            return JsonResponse({"error": f"Failed to download PDF from S3: {str(s3_error)}"}, status=500)
        
        # STEP 1: Extract answers from PDF
        print("Starting extraction process...")
        extracted_data = process_answer_sheet_with_ai(temp_pdf_path, exam_id, register_number)
        
        if not extracted_data or not extracted_data.get("answers"):
            return JsonResponse({"error": "Failed to extract answers from the PDF"}, status=400)
        
        answers = extracted_data.get("answers", [])
        print(f"Successfully extracted {len(answers)} answers from PDF")
        
        # Update answer sheet with extracted answers
        if "student_id" in answer_sheet:
            answer_sheet_collection.update_one(
                {"_id": ObjectId(answer_sheet_id)},
                {"$set": {
                    "extracted_answers": answers,
                    "processing_status": "extraction_complete",
                    "processed_at": datetime.now()
                }}
            )
        else:
            # Handle nested document structure
            for subject_idx, subject in enumerate(answer_sheet.get("subjects", [])):
                if subject.get("subject_code") == subject_code:
                    for student_idx, student in enumerate(subject.get("students", [])):
                        if student.get("student_id") == register_number:
                            answer_sheet_collection.update_one(
                                {"_id": ObjectId(answer_sheet_id)},
                                {"$set": {
                                    f"subjects.{subject_idx}.students.{student_idx}.extracted_answers": answers,
                                    f"subjects.{subject_idx}.students.{student_idx}.processing_status": "extraction_complete",
                                    f"subjects.{subject_idx}.students.{student_idx}.processed_at": datetime.now()
                                }}
                            )
                            break
                    break
        
        # STEP 2: Evaluate the extracted answers
        print("Starting evaluation process...")
        try:
            # Get student info
            student = student_collection.find_one({
                "$or": [
                    {"register_number": register_number},
                    {"roll_number": register_number}
                ]
            })
            
            if not student:
                print(f"Student with register number {register_number} not found")
                return JsonResponse({
                    "error": "Student not found for evaluation",
                    "extracted_answers": len(answers)
                }, status=400)
            
            # Get subject name
            subject_name = ""
            if "subject_name" in answer_sheet:
                subject_name = answer_sheet.get("subject_name", "")
            else:
                for subject in answer_sheet.get("subjects", []):
                    if subject.get("subject_code") == subject_code:
                        subject_name = subject.get("subject_name", "")
                        break
            
            if not subject_name:
                for subject in exam_doc.get("subjects", []):
                    if subject.get("subject_code") == subject_code:
                        subject_name = subject.get("subject_name", "")
                        break
            
            # Map of extracted answers for quick lookup
            answer_map = {ans.get("question_no"): ans for ans in answers}
            
            # Process each mapped question
            evaluated_answers = []
            total_marks = 0
            
            for q in mapped_questions:
                q_no = q.get("question_no")
                expected_answer = q.get("answer_text", "")
                keywords = q.get("keywords", [])
                max_marks = q.get("marks", 0)
                bloom_level = q.get("bloom_level", "")
                co = q.get("CO", "")
                
                print(f"Evaluating question {q_no}")
                
                # Get rubric for this mark
                rubric_doc = rubrics_collection.find_one({"mark_category": str(max_marks)})
                
                def get_rubric_items(rubric_doc, bloom_level):
                    if not rubric_doc:
                        return []
                    for criterion in rubric_doc.get("criteria", []):
                        if criterion.get("bloom_level", "").lower() == bloom_level.lower() or criterion.get("Short_Name", "").lower() == bloom_level.lower():
                            return criterion.get("items", [])
                    return []
                
                rubric_items = get_rubric_items(rubric_doc, bloom_level)
                
                # Find matching student answer
                student_answer_obj = None
                for key in [q_no, q_no.replace(')', ''), q_no + ')']:
                    if key in answer_map:
                        student_answer_obj = answer_map[key]
                        break
                
                if student_answer_obj:
                    student_answer = student_answer_obj.get("answer_text", "")
                    print(f"Found student answer for question {q_no}, length: {len(student_answer)}")
                    
                    try:
                        # Choose evaluation method based on marks
                        if max_marks >= 13:
                            try:
                                print(f"Using Gemini API for evaluation: question {q_no}")
                                marks_awarded, justification, feedback, reduction_reasons, ai_recommendation, rubrick_guidelines, rubrick_marks = call_gemini_api(
                                    student_answer, expected_answer, keywords, max_marks, rubric_items
                                )
                                method_used = "gemini"
                            except Exception as eval_error:
                                print(f"Gemini API failed, using fallback: {str(eval_error)}")
                                marks_awarded, justification, feedback, reduction_reasons, ai_recommendation, rubrick_guidelines, rubrick_marks = keyword_based_scoring(
                                    student_answer, keywords, max_marks, rubric_items
                                )
                                method_used = "gemini_fallback"
                        else:
                            print(f"Using keyword scoring for evaluation: question {q_no}")
                            marks_awarded, justification, feedback, reduction_reasons, ai_recommendation, rubrick_guidelines, rubrick_marks = keyword_based_scoring(
                                student_answer, keywords, max_marks, rubric_items
                            )
                            method_used = "keyword"
                        
                        print(f"Evaluation completed for question {q_no}, marks: {marks_awarded}")
                    except Exception as eval_error:
                        print(f"Error in evaluation of question {q_no}: {str(eval_error)}")
                        marks_awarded = 0
                        method_used = "error"
                        justification = f"Evaluation error: {str(eval_error)}"
                        feedback = "No feedback available due to evaluation error."
                        reduction_reasons = "Evaluation error"
                        ai_recommendation = "Please review this question manually."
                        rubrick_guidelines = []
                        rubrick_marks = []
                else:
                    print(f"No student answer found for question {q_no}")
                    marks_awarded = 0
                    method_used = "skipped"
                    justification = "Answer not found in extracted content"
                    feedback = "No feedback available."
                    reduction_reasons = "Answer not found in extracted content"
                    ai_recommendation = "No AI recommendation available."
                    rubrick_guidelines = []
                    rubrick_marks = []
                
                # Add marks to total
                total_marks += marks_awarded
                
                # Add to evaluated answers
                evaluated_answers.append({
                    "question_no": q_no,
                    "bloom_level": bloom_level,
                    "marks_awarded": marks_awarded,
                    "method_used": method_used,
                    "justification": justification,
                    "feedback": feedback,
                    "reduction_reasons": reduction_reasons,
                    "ai_recommendation": ai_recommendation,
                    "rubric_items": rubrick_guidelines,
                    "rubric_marks": rubrick_marks,
                    "co": co
                })
            
            print(f"All questions evaluated. Total marks: {total_marks}")
            
            # Create student result data
            student_result_data = {
                "name": student.get("name"),
                "evaluated_answers": evaluated_answers,
                "total_marks": total_marks,
                "created_at": datetime.now(),
                "answer_sheet_id": str(answer_sheet_id),
                "answer_sheet_file_url": file_url,
                "subject_code": subject_code,
                "subject_name": subject_name,
                "register_number": register_number,
                "exam_type": exam_type
            }
            
            # Save to results collection
            existing_exam_result = results_collection.find_one({
                "$or": [
                    {"exam_id": exam_id},
                    {"exam_id": str(exam_id)}
                ]
            })
            
            # Insert or update results
            print("Saving evaluation results to database...")
            result_id = None
            
            if existing_exam_result:
                result_id = existing_exam_result["_id"]
                if "results" not in existing_exam_result:
                    # Create initial results structure
                    results_collection.update_one(
                        {"_id": result_id},
                        {"$set": {
                            "results": {
                                "subjects": [
                                    {
                                        "subject_code": subject_code,
                                        "subject_name": subject_name,
                                        "students": [student_result_data]
                                    }
                                ]
                            },
                            "exam_type": exam_type
                        }}
                    )
                else:
                    # Update existing results
                    subjects = existing_exam_result.get("results", {}).get("subjects", [])
                    subject_exists = False
                    subject_idx = -1
                    
                    # Find if subject exists
                    for idx, subj in enumerate(subjects):
                        if subj.get("subject_code") == subject_code:
                            subject_exists = True
                            subject_idx = idx
                            break
                    
                    if subject_exists:
                        # Find if student exists
                        students = subjects[subject_idx].get("students", [])
                        student_exists = False
                        student_idx = -1
                        
                        for idx, stud in enumerate(students):
                            if stud.get("register_number") == register_number:
                                student_exists = True
                                student_idx = idx
                                break
                        
                        if student_exists:
                            # Update existing student
                            results_collection.update_one(
                                {"_id": result_id},
                                {"$set": {
                                    f"results.subjects.{subject_idx}.students.{student_idx}": student_result_data,
                                    "exam_type": exam_type
                                }}
                            )
                        else:
                            # Add new student
                            results_collection.update_one(
                                {"_id": result_id},
                                {
                                    "$push": {
                                        f"results.subjects.{subject_idx}.students": student_result_data
                                    },
                                    "$set": {
                                        "exam_type": exam_type
                                    }
                                }
                            )
                    else:
                        # Add new subject
                        results_collection.update_one(
                            {"_id": result_id},
                            {
                                "$push": {
                                    "results.subjects": {
                                        "subject_code": subject_code,
                                        "subject_name": subject_name,
                                        "students": [student_result_data]
                                    }
                                },
                                "$set": {
                                    "exam_type": exam_type
                                }
                            }
                        )
            else:
                # Create new results document
                result = results_collection.insert_one({
                    "exam_id": str(exam_id),
                    "exam_type": exam_type,
                    "results": {
                        "subjects": [
                            {
                                "subject_code": subject_code,
                                "subject_name": subject_name,
                                "students": [student_result_data]
                            }
                        ]
                    }
                })
                result_id = result.inserted_id
            
            # Update answer sheet with evaluation status
            print("Updating answer sheet status to 'evaluated'")
            if "student_id" in answer_sheet:
                answer_sheet_collection.update_one(
                    {"_id": ObjectId(answer_sheet_id)},
                    {"$set": {
                        "evaluation_status": "completed",
                        "evaluation_id": result_id,
                        "total_marks": total_marks,
                        "evaluated_at": datetime.now(),
                        "is_evaluated": True
                    }}
                )
            else:
                for subject_idx, subject in enumerate(answer_sheet.get("subjects", [])):
                    if subject.get("subject_code") == subject_code:
                        for student_idx, student in enumerate(subject.get("students", [])):
                            if student.get("student_id") == register_number:
                                answer_sheet_collection.update_one(
                                    {"_id": ObjectId(answer_sheet_id)},
                                    {"$set": {
                                        f"subjects.{subject_idx}.students.{student_idx}.evaluation_status": "completed",
                                        f"subjects.{subject_idx}.students.{student_idx}.evaluation_id": result_id,
                                        f"subjects.{subject_idx}.students.{student_idx}.total_marks": total_marks,
                                        f"subjects.{subject_idx}.students.{student_idx}.evaluated_at": datetime.now(),
                                        f"subjects.{subject_idx}.students.{student_idx}.is_evaluated": True
                                    }}
                                )
                                break
                        break
            
            # Clean up
            try:
                os.remove(temp_pdf_path)
                print(f"Temporary PDF file {temp_pdf_path} deleted")
            except Exception as cleanup_error:
                print(f"Warning: Failed to delete temp file {temp_pdf_path}: {str(cleanup_error)}")
            
            return JsonResponse({
                "message": "Answer sheet processed and evaluated successfully",
                "extracted_answers": len(answers),
                "evaluation_id": str(result_id),
                "total_marks": total_marks
            }, status=200)
            
        except Exception as eval_error:
            print(f"Error in evaluation process: {str(eval_error)}")
            print(f"Traceback: {traceback.format_exc()}")
            
            return JsonResponse({
                "message": "Answer sheet processed but evaluation failed",
                "extracted_answers": len(answers),
                "evaluation_error": str(eval_error)
            }, status=200)
        
    except Exception as e:
        print(f"Critical error in process_and_evaluate_answer_sheet: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return JsonResponse({"error": str(e)}, status=500)  

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from bson.objectid import ObjectId
import json
import uuid
import threading
import time
from datetime import datetime
import traceback

# In-memory storage for tracking validation jobs
validation_jobs = {}

@csrf_exempt
def job_status(request, job_id):
    """
    Returns the status of a validation job.
    """
    if job_id not in validation_jobs:
        return JsonResponse({"error": "Job not found"}, status=404)

    job = validation_jobs[job_id]
    response = {
        "job_id": job_id,
        "status": job["status"],
        "progress": {
            "total": job["results"]["total"],
            "completed": job["results"]["completed"],
            "successful": len(job["results"]["successful"]),
            "failed": len(job["results"]["failed"]),
            "percent_complete": round((job["results"]["completed"] / job["results"]["total"] * 100) if job["results"]["total"] > 0 else 0, 2)
        },
        "results": {
            "successful": job["results"]["successful"],
            "failed": job["results"]["failed"]
        },
        "error": job.get("error", "")
    }
    return JsonResponse(response, status=200)

@csrf_exempt
def validate_all_answer_sheets(request):
    """
    Initiates validation of all unevaluated answer sheets for a given exam_id and subject_code.
    Processes evaluations sequentially in a background thread, stopping on rate limit errors.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get("exam_id")
        subject_code = data.get("subject_code")

        if not all([exam_id, subject_code]):
            return JsonResponse({"error": "Missing exam_id or subject_code"}, status=400)

        # Fetch documents with unevaluated answer sheets in the nested structure
        answer_sheets = answer_sheet_collection.find({
            "exam_id": exam_id,
            "subjects": {
                "$elemMatch": {
                    "subject_code": subject_code,
                    "students": {
                        "$elemMatch": {
                            "is_evaluated": False,
                            "file_url": {"$exists": True}
                        }
                    }
                }
            }
        })

        answer_sheet_ids = []
        for sheet in answer_sheets:
            for subject in sheet.get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    for student in subject.get("students", []):
                        if not student.get("is_evaluated", False) and student.get("file_url"):
                            answer_sheet_ids.append({
                                "answer_sheet_id": str(sheet["_id"]),
                                "register_number": student.get("student_id"),
                                "subject_code": subject_code
                            })

        if not answer_sheet_ids:
            return JsonResponse({"error": "No unevaluated answer sheets found"}, status=400)

        # Create a job ID for tracking
        job_id = str(uuid.uuid4())

        # Initialize job tracking
        validation_jobs[job_id] = {
            "exam_id": exam_id,
            "subject_code": subject_code,
            "answer_sheets": answer_sheet_ids,
            "status": "processing",
            "timestamp": datetime.now(),
            "results": {
                "total": len(answer_sheet_ids),
                "completed": 0,
                "successful": [],
                "failed": []
            },
            "api_usage": {
                "total_calls": 0,
                "successful_calls": 0,
                "rate_limited_calls": 0,
                "last_call_time": None
            }
        }

        # Start validation process in background
        validation_thread = threading.Thread(
            target=process_validate_all,
            args=(job_id,)
        )
        validation_thread.daemon = True
        validation_thread.start()

        # Return immediate response
        return JsonResponse({
            "job_id": job_id,
            "message": "Validation processing started",
            "status": "processing",
            "sheets_to_process": len(answer_sheet_ids)
        }, status=200)

    except Exception as e:
        print(f"Error in validate_all_answer_sheets: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)

def process_validate_all(job_id):
    """
    Processes validation of all unevaluated answer sheets sequentially,
    with 6-second delays to avoid API rate limits, stopping on rate limit errors.
    """
    if job_id not in validation_jobs:
        return

    job = validation_jobs[job_id]
    answer_sheets = job["answer_sheets"]

    try:
        # Process each answer sheet sequentially
        for i, sheet_info in enumerate(answer_sheets):
            answer_sheet_id = sheet_info["answer_sheet_id"]
            register_number = sheet_info["register_number"]
            subject_code = sheet_info["subject_code"]

            try:
                print(f"Processing answer sheet {i+1}/{len(answer_sheets)}: {answer_sheet_id} for student {register_number}")

                # Enforce a 6-second delay between evaluations (except for the first sheet)
                if i > 0:
                    print(f"Waiting 6 seconds before processing next answer sheet...")
                    time.sleep(6)

                # Record API call start time
                job["api_usage"]["last_call_time"] = datetime.now().isoformat()
                job["api_usage"]["total_calls"] += 1

                # Create mock request payload
                payload = {
                    "exam_id": job["exam_id"],
                    "register_number": register_number,
                    "subject_code": subject_code,
                    "answer_sheet_id": answer_sheet_id
                }

                # Mock request class
                class MockRequest:
                    def __init__(self, payload):
                        self.body = json.dumps(payload).encode('utf-8')
                        self.method = "POST"

                mock_request = MockRequest(payload)
                evaluation_response = process_and_evaluate_answer_sheet(mock_request)

                # Parse response
                response_data = json.loads(evaluation_response.content)

                if "error" in response_data:
                    error_msg = response_data["error"]
                    print(f"❌ Evaluation error for sheet {answer_sheet_id}: {error_msg}")
                    result = {
                        "answer_sheet_id": answer_sheet_id,
                        "register_number": register_number,
                        "success": False,
                        "error": error_msg
                    }
                    job["results"]["failed"].append(result)
                    if "rate limit" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
                        job["api_usage"]["rate_limited_calls"] += 1
                        job["status"] = "failed"
                        job["error"] = "Stopped due to API rate limit"
                        print(f"⚠️ Rate limit hit for sheet {answer_sheet_id}, stopping processing")
                        return  # Stop processing on rate limit error
                else:
                    job["api_usage"]["successful_calls"] += 1
                    job["results"]["successful"].append({
                        "answer_sheet_id": answer_sheet_id,
                        "register_number": register_number,
                        "success": True,
                        "total_marks": response_data.get("total_marks", 0),
                        "evaluation_id": response_data.get("evaluation_id", ""),
                        "extracted_answers": response_data.get("extracted_answers", 0)
                    })
                    print(f"✅ Successfully evaluated sheet {answer_sheet_id} - marks: {response_data.get('total_marks', 0)}")

            except Exception as e:
                error_msg = str(e)
                print(f"❌ Error processing sheet {answer_sheet_id}: {error_msg}")
                job["results"]["failed"].append({
                    "answer_sheet_id": answer_sheet_id,
                    "register_number": register_number,
                    "success": False,
                    "error": error_msg
                })
                if "rate limit" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
                    job["api_usage"]["rate_limited_calls"] += 1
                    job["status"] = "failed"
                    job["error"] = "Stopped due to API rate limit"
                    print(f"⚠️ Rate limit exception: {error_msg}, stopping processing")
                    return  # Stop processing on rate limit error

            # Update progress
            job["results"]["completed"] += 1
            print(f"Progress: {job['results']['completed']}/{job['results']['total']} sheets processed")

        # Update job status
        job["status"] = "completed"
        print(f"✅ Job {job_id} completed: {len(job['results']['successful'])} successful, {len(job['results']['failed'])} failed")

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        print(f"❌ Job {job_id} failed: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")           
# Improved function to get all students for a given exam_id and subject_code in results collection
@csrf_exempt
def get_data_in_results(request):
    """
    Retrieves all students and their results for a given exam_id and subject_code,
    and includes department information for each student.
    Returns a list of student objects for the subject, including staff mark, AI mark, is_evaluated, and department.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get("exam_id")
        subject_code = data.get("subject_code")

        if not all([exam_id, subject_code]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Find the result document for the exam
        existing_exam_result = results_collection.find_one({"exam_id": str(exam_id)})
        if not existing_exam_result:
            return JsonResponse({"error": "Exam result not found"}, status=404)

        # Find the subject in the results
        subjects = existing_exam_result.get("results", {}).get("subjects", [])
        for subject in subjects:
            if subject.get("subject_code") == subject_code:
                students = subject.get("students", [])
                result_students = []
                for stu in students:
                    reg_no = stu.get("register_number", "")
                    # Fetch department info from student_collection
                    student_profile = student_collection.find_one({"register_number": reg_no})
                    department = student_profile.get("department", "") if student_profile else ""
                    result_students.append({
                        "register_number": reg_no,
                        "name": stu.get("name", ""),
                        "department": department,
                        "staff_mark": stu.get("staff_mark"),
                        "ai_mark": stu.get("total_marks", 0),
                        "is_evaluated": True if stu.get("evaluated_answers") else False
                    })
                return JsonResponse({"students": result_students}, status=200)

        return JsonResponse({"error": "Subject not found in results"}, status=404)

    except Exception as e:
        print(f"Error in get_data_in_results: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def get_data_in_results(request):
    """
    Retrieves all students and their results for a given exam_id and subject_code.
    Returns a list of student objects for the subject, including staff mark, AI mark, and is_evaluated.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get("exam_id")
        subject_code = data.get("subject_code")
        print(f"Exam ID: {exam_id}, Subject Code: {subject_code}")

        if not all([exam_id, subject_code]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Find the result document for the exam
        existing_exam_result = results_collection.find_one({"exam_id": str(exam_id)})
        if not existing_exam_result:
            return JsonResponse({"error": "Exam result not found"}, status=404)

        # Find the subject in the results
        subjects = existing_exam_result.get("results", {}).get("subjects", [])
        for subject in subjects:
            if subject.get("subject_code", "").strip().upper() == subject_code.strip().upper():
                students = subject.get("students", [])
                if not students:
                    return JsonResponse({
                        "message": "No results were published yet. Try to validate some papers after uploading the answer sheet or check if you've uploaded it."
                    }, status=200)

                result_students = []
                for stu in students:
                    student_profile = student_collection.find_one({"register_number": stu.get("register_number", "")})
                    student_id = str(student_profile.get("_id")) if student_profile and "_id" in student_profile else ""

                    result_students.append({
                        "register_number": stu.get("register_number", ""),
                        "id": student_id,
                        "name": stu.get("name", ""),
                        "staff_mark": stu.get("staff_mark"),
                        "ai_mark": stu.get("total_marks", 0),
                        "is_evaluated": bool(stu.get("evaluated_answers"))
                    })

                return JsonResponse({"students": result_students}, status=200)

        # Subject not found — return friendly empty message
        return JsonResponse({
            "message": "No results were published yet. Try to validate some papers after uploading the answer sheet or check if you've uploaded it."
        }, status=200)

    except Exception as e:
        print(f"Error in get_data_in_results: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def update_staff_mark(request):
    """
    Updates the staff mark for a student's answer sheet in the database.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # First try to parse JSON data
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            # Fallback to form data
            data = {
                "exam_id": request.POST.get("exam_id"),
                "register_number": request.POST.get("register_number"),
                "subject_code": request.POST.get("subject_code"),
                "staff_mark": request.POST.get("staff_mark")
            }
            # Convert staff_mark to int/float if it's a string
            if isinstance(data["staff_mark"], str) and data["staff_mark"].strip():
                try:
                    data["staff_mark"] = float(data["staff_mark"])
                    if data["staff_mark"].is_integer():
                        data["staff_mark"] = int(data["staff_mark"])
                except ValueError:
                    return JsonResponse({"error": "Invalid staff mark format"}, status=400)
        
        exam_id = data.get("exam_id")
        register_number = data.get("register_number")
        subject_code = data.get("subject_code")
        staff_mark = data.get("staff_mark")

        print(f"Received staff mark update: {exam_id=}, {register_number=}, {subject_code=}, {staff_mark=}")

        if not all([exam_id, register_number, subject_code]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Validate staff_mark
        if staff_mark is not None and (not isinstance(staff_mark, (int, float)) or staff_mark < 0 or staff_mark > 100):
            return JsonResponse({"error": "Invalid staff mark. Must be between 0 and 100."}, status=400)

        # Try both ObjectId and string format for exam_id
        exam_id_query = {"$or": [{"exam_id": exam_id}, {"exam_id": str(exam_id)}]}
        if ObjectId.is_valid(exam_id):
            exam_id_query["$or"].append({"exam_id": ObjectId(exam_id)})

        # Find the existing result document
        existing_exam_result = results_collection.find_one(exam_id_query)

        if not existing_exam_result:
            print(f"Exam result not found for query: {exam_id_query}")
            # Try to find by directly using _id
            if ObjectId.is_valid(exam_id):
                existing_exam_result = results_collection.find_one({"_id": ObjectId(exam_id)})
                if not existing_exam_result:
                    return JsonResponse({"error": "Exam result not found"}, status=404)
            else:
                return JsonResponse({"error": "Exam result not found"}, status=404)

        # Navigate through the results to find the student
        updated = False
        subjects = existing_exam_result.get("results", {}).get("subjects", [])
        for i, subject in enumerate(subjects):
            if subject.get("subject_code") == subject_code:
                for j, student in enumerate(subject.get("students", [])):
                    if student.get("register_number") == register_number:
                        # Update staff mark
                        update_result = results_collection.update_one(
                            {"_id": existing_exam_result["_id"]},
                            {"$set": {
                                f"results.subjects.{i}.students.{j}.staff_mark": staff_mark
                            }}
                        )
                        updated = True
                        break
                break

        if not updated:
            return JsonResponse({"error": "Student or subject not found in results"}, status=404)

        # Update answer sheet collection if needed
        answer_sheet_query = {
            "$or": [
                {"exam_id": exam_id},
                {"exam_id": str(exam_id)}
            ]
        }
        if ObjectId.is_valid(exam_id):
            answer_sheet_query["$or"].append({"exam_id": ObjectId(exam_id)})
            
        answer_sheets = list(answer_sheet_collection.find(answer_sheet_query))
        
        for answer_sheet in answer_sheets:
            updated_answer_sheet = False
            
            # Handle nested structure
            if "subjects" in answer_sheet:
                for s_idx, subj in enumerate(answer_sheet.get("subjects", [])):
                    if subj.get("subject_code") == subject_code:
                        for st_idx, stu in enumerate(subj.get("students", [])):
                            if stu.get("student_id") == register_number:
                                answer_sheet_collection.update_one(
                                    {"_id": answer_sheet["_id"]},
                                    {"$set": {
                                        f"subjects.{s_idx}.students.{st_idx}.staff_mark": staff_mark,
                                        f"subjects.{s_idx}.students.{st_idx}.staff_mark_updated_at": datetime.now()
                                    }}
                                )
                                updated_answer_sheet = True
                                break
            
            # Handle flat structure
            elif answer_sheet.get("subject_code") == subject_code and answer_sheet.get("student_id") == register_number:
                answer_sheet_collection.update_one(
                    {"_id": answer_sheet["_id"]},
                    {"$set": {
                        "staff_mark": staff_mark,
                        "staff_mark_updated_at": datetime.now()
                    }}
                )
                updated_answer_sheet = True
                
            if updated_answer_sheet:
                print(f"Updated answer sheet {answer_sheet['_id']} with staff mark {staff_mark}")

        return JsonResponse({
            "message": "Staff mark updated successfully",
            "staff_mark": staff_mark
        }, status=200)

    except Exception as e:
        import traceback
        print(f"Error in update_staff_mark: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({"error": str(e)}, status=500)

#=============================================RUBRICKS=========================================

@csrf_exempt
def update_subject_rubrics(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        exam_id = data.get('exam_id')
        subject_code = data.get('subject_code')
        rubrics = data.get('rubrics', '')

        if not exam_id or not subject_code:
            return JsonResponse({'error': 'Missing exam_id or subject_code'}, status=400)

        # Validate exam_id and fetch exam document
        try:
            exam = exam_collection.find_one({'_id': ObjectId(exam_id)})
            if not exam:
                return JsonResponse({'error': 'Exam not found'}, status=404)
            
            # Check if subject_code exists in exam's subjects
            subject_exists = any(s['subject_code'] == subject_code for s in exam.get('subjects', []))
            if not subject_exists:
                return JsonResponse({'error': f'Subject {subject_code} not found in exam'}, status=404)
        except Exception as e:
            return JsonResponse({'error': 'Invalid exam_id format'}, status=400)

        # Update exam_collection
        result = exam_collection.update_one(
            {'_id': ObjectId(exam_id), 'subjects.subject_code': subject_code},
            {'$set': {
                'subjects.$.rubrics': rubrics,
                'updated_at': datetime.now()
            }}
        )

        if result.modified_count == 0:
            return JsonResponse({'error': 'Failed to update rubric in exam'}, status=500)

        # Use exam_id (ObjectId as string) to update exam_mapped_questions_collection
        result = exam_mapped_questions_collection.update_one(
            {'exam_id': exam_id, 'subject_code': subject_code},
            {'$set': {
                'rubrics': rubrics,
                'updated_at': datetime.now()
            }},
            upsert=True  # Create if not exists
        )

        print(f"Updated rubrics for exam_id: {exam_id}, subject_code: {subject_code}, rubrics: {rubrics}")
        return JsonResponse({
            'message': 'Rubrics updated successfully',
            'exam_id': exam_id
        }, status=200)

    except Exception as e:
        print(f"Error updating rubrics: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def create_rubric(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        category = data.get('category')
        bloom_level = data.get('bloom_level')
        template_id = data.get('template_id')
        criteria = data.get('criteria')

        if not category or not bloom_level or not template_id or not criteria:
            return JsonResponse({'error': 'Missing category, bloom_level, template_id, or criteria'}, status=400)
        
        if category not in ['2-mark', '13-mark', '14-mark']:
            return JsonResponse({'error': 'Category must be 2-mark, 13-mark, or 14-mark'}, status=400)

        if bloom_level not in ['Remember', 'Understand', 'Apply', 'Analyze', 'Evaluate', 'Create']:
            return JsonResponse({'error': 'Invalid Bloom level'}, status=400)

        valid_templates = {
            'Remember': ['R1', 'R2', 'R3'],
            'Understand': ['U1', 'U2', 'U3'],
            'Apply': ['AP-1', 'AP-2', 'AP-3'],
            'Analyze': ['A1', 'A2', 'A3'],
            'Evaluate': ['E1', 'E2', 'E3'],
            'Create': ['C1', 'C2', 'C3']
        }
        if template_id not in valid_templates[bloom_level]:
            return JsonResponse({'error': f'Invalid template_id for {bloom_level}'}, status=400)

        mark_category = category.split('-')[0]

        # Validate criteria
        if not isinstance(criteria, list):
            return JsonResponse({'error': 'Criteria must be a list'}, status=400)
        for item in criteria:
            if not item.get('description') or not isinstance(item.get('marks'), (int, float)) or item['marks'] <= 0:
                return JsonResponse({'error': 'Each criteria item must have a description and positive marks'}, status=400)

        # Define Bloom level mappings
        BLOOM_MAPPINGS = {
            'Remember': {'Short_Name': 'REM', 'Level': 'L1'},
            'Understand': {'Short_Name': 'UND', 'Level': 'L2'},
            'Apply': {'Short_Name': 'APP', 'Level': 'L3'},
            'Analyze': {'Short_Name': 'ANA', 'Level': 'L4'},
            'Evaluate': {'Short_Name': 'EVA', 'Level': 'L5'},
            'Create': {'Short_Name': 'CRT', 'Level': 'L6'},
        }

        # Check existing templates for this category and bloom_level
        existing_templates = rubrics_collection.count_documents({
            'mark_category': mark_category,
            'bloom_level': bloom_level
        })
        if existing_templates >= 3 and template_id not in [doc['template_id'] for doc in rubrics_collection.find({
            'mark_category': mark_category,
            'bloom_level': bloom_level
        })]:
            return JsonResponse({'error': f'Maximum 3 templates allowed for {bloom_level} in {category}'}, status=400)

        # Check if rubric with same mark_category, bloom_level, and template_id exists
        existing_rubric = rubrics_collection.find_one({
            'mark_category': mark_category,
            'bloom_level': bloom_level,
            'template_id': template_id
        })

        if existing_rubric:
            # Update existing rubric
            result = rubrics_collection.update_one(
                {'_id': existing_rubric['_id']},
                {'$set': {
                    'criteria': criteria,
                    'updated_at': datetime.now()
                }}
            )
            if result.modified_count == 0:
                return JsonResponse({'error': 'Failed to update rubric'}, status=500)
            updated_rubric = rubrics_collection.find_one({'_id': existing_rubric['_id']})
            updated_rubric['_id'] = str(updated_rubric['_id'])
            print(f"Updated rubric: {updated_rubric}")
            return JsonResponse({'message': 'Rubric updated successfully', 'rubric': updated_rubric}, status=200)
        else:
            # Create new rubric
            rubric = {
                'mark_category': mark_category,
                'bloom_level': bloom_level,
                'template_id': template_id,
                'criteria': criteria,
                'Short_Name': BLOOM_MAPPINGS[bloom_level]['Short_Name'],
                'Level': BLOOM_MAPPINGS[bloom_level]['Level'],
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            }
            result = rubrics_collection.insert_one(rubric)
            rubric['_id'] = str(result.inserted_id)
            print(f"Created rubric: {rubric}")
            return JsonResponse({'message': 'Rubric created successfully', 'rubric': rubric}, status=201)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def update_rubric(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        rubric_id = data.get('rubric_id')
        category = data.get('category')
        bloom_level = data.get('bloom_level')
        template_id = data.get('template_id')
        criteria = data.get('criteria')

        if not rubric_id or not category or not bloom_level or not template_id or not criteria:
            return JsonResponse({'error': 'Missing rubric_id, category, bloom_level, template_id, or criteria'}, status=400)

        if category not in ['2-mark', '13-mark', '14-mark']:
            return JsonResponse({'error': 'Category must be 2-mark, 13-mark, or 14-mark'}, status=400)

        if bloom_level not in ['Remember', 'Understand', 'Apply', 'Analyze', 'Evaluate', 'Create']:
            return JsonResponse({'error': 'Invalid Bloom level'}, status=400)

        valid_templates = {
            'Remember': ['R1', 'R2', 'R3'],
            'Understand': ['U1', 'U2', 'U3'],
            'Apply': ['AP-1', 'AP-2', 'AP-3'],
            'Analyze': ['A1', 'A2', 'A3'],
            'Evaluate': ['E1', 'E2', 'E3'],
            'Create': ['C1', 'C2', 'C3']
        }
        if template_id not in valid_templates[bloom_level]:
            return JsonResponse({'error': f'Invalid template_id for {bloom_level}'}, status=400)

        mark_category = category.split('-')[0]

        # Validate criteria
        if not isinstance(criteria, list):
            return JsonResponse({'error': 'Criteria must be a list'}, status=400)
        for item in criteria:
            if not item.get('description') or not isinstance(item.get('marks'), (int, float)) or item['marks'] <= 0:
                return JsonResponse({'error': 'Each criteria item must have a description and positive marks'}, status=400)

        # Define Bloom level mappings
        BLOOM_MAPPINGS = {
            'Remember': {'Short_Name': 'REM', 'Level': 'L1'},
            'Understand': {'Short_Name': 'UND', 'Level': 'L2'},
            'Apply': {'Short_Name': 'APP', 'Level': 'L3'},
            'Analyze': {'Short_Name': 'ANA', 'Level': 'L4'},
            'Evaluate': {'Short_Name': 'EVA', 'Level': 'L5'},
            'Create': {'Short_Name': 'CRT', 'Level': 'L6'},
        }

        result = rubrics_collection.update_one(
            {'_id': ObjectId(rubric_id)},
            {'$set': {
                'mark_category': mark_category,
                'bloom_level': bloom_level,
                'template_id': template_id,
                'criteria': criteria,
                'Short_Name': BLOOM_MAPPINGS[bloom_level]['Short_Name'],
                'Level': BLOOM_MAPPINGS[bloom_level]['Level'],
                'updated_at': datetime.now()
            }}
        )

        if result.modified_count == 0:
            return JsonResponse({'error': 'Rubric not found or no changes made'}, status=404)

        print(f"Updated rubric_id: {rubric_id}, mark_category: {mark_category}, bloom_level: {bloom_level}, template_id: {template_id}")
        return JsonResponse({'message': 'Rubric updated successfully'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_rubrics(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method is allowed'}, status=405)

    try:
        rubrics = list(rubrics_collection.find())
        for rubric in rubrics:
            rubric['_id'] = str(rubric['_id'])
            rubric['created_at'] = rubric['created_at'].isoformat()
            rubric['updated_at'] = rubric['updated_at'].isoformat()
        print(f"Fetched {len(rubrics)} rubrics")
        return JsonResponse({'rubrics': rubrics}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def delete_rubric(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed'}, status=405)

    try:
        data = json.loads(request.body)
        rubric_id = data.get('rubric_id')

        if not rubric_id:
            return JsonResponse({'error': 'Missing rubric_id'}, status=400)

        result = rubrics_collection.delete_one({'_id': ObjectId(rubric_id)})

        if result.deleted_count == 0:
            return JsonResponse({'error': 'Rubric not found'}, status=404)

        print(f"Deleted rubric_id: {rubric_id}")
        return JsonResponse({'message': 'Rubric deleted successfully'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

#=============================================================================================
    
@csrf_exempt
def get_answer_sheet_status(request, answer_sheet_id):
    """
    Get the status of an answer sheet by its ID.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Find the answer sheet
        answer_sheet = answer_sheet_collection.find_one({"_id": ObjectId(answer_sheet_id)})
        
        if not answer_sheet:
            return JsonResponse({"error": "Answer sheet not found"}, status=404)
        
        # Return status information
        status_info = {
            "id": str(answer_sheet["_id"]),
            "student_id": answer_sheet.get("student_id"),
            "exam_id": str(answer_sheet.get("exam_id")),
            "processing_status": answer_sheet.get("processing_status", "pending"),
            "submitted_at": answer_sheet.get("submitted_at", "").isoformat() if answer_sheet.get("submitted_at") else None,
            "processed_at": answer_sheet.get("processed_at", "").isoformat() if answer_sheet.get("processed_at") else None
        }
        
        # Include evaluation data if available
        if answer_sheet.get("evaluation_status") == "completed" and answer_sheet.get("evaluation_id"):
            status_info["evaluation_status"] = "completed"
            status_info["evaluation_id"] = str(answer_sheet.get("evaluation_id"))
            status_info["total_marks"] = answer_sheet.get("total_marks")
            status_info["evaluated_at"] = answer_sheet.get("evaluated_at", "").isoformat() if answer_sheet.get("evaluated_at") else None
        
        return JsonResponse(status_info, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def process_answer_sheet(request):
    """
    Simple endpoint for testing answer sheet processing
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    # Simple response to confirm the endpoint is working
    return JsonResponse({"message": "Answer sheet processing endpoint is working"}, status=200)

@csrf_exempt
def simple_process_endpoint(request):
    """
    Very simple endpoint for testing routing
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    # Just return a success response to test if the endpoint is accessible
    return JsonResponse({"message": "Simple processing endpoint is working"}, status=200)

logger = logging.getLogger(__name__)
@csrf_exempt
def bulk_upload_answer_sheets(request):
    """
    Upload multiple answer sheets at once via a ZIP file.
    Each file must follow naming convention: register_number_subject_code.pdf
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        # Log request details for debugging
        print(f"Received bulk upload request with content type: {request.content_type}")
        print(f"Request POST keys: {list(request.POST.keys())}")
        print(f"Request FILES keys: {list(request.FILES.keys())}")
        
        # Extract parameters
        exam_id = request.POST.get("exam_id")
        
        if not exam_id:
            return JsonResponse({"error": "Missing exam_id parameter"}, status=400)
        
        # Check if 'zip_file' exists in request.FILES
        if 'zip_file' not in request.FILES:
            # If not, check if any file was uploaded
            if len(request.FILES) > 0:
                # Get the first available file field
                file_field = list(request.FILES.keys())[0]
                archive_file = request.FILES[file_field]
                print(f"Found file with field name '{file_field}' instead of 'zip_file'")
            else:
                return JsonResponse({"error": "ZIP file not provided"}, status=400)
        else:
            archive_file = request.FILES['zip_file']
        
        # Check file type
        file_extension = os.path.splitext(archive_file.name)[1].lower()
        if file_extension != '.zip':
            return JsonResponse({
                "error": f"Invalid file format: {file_extension}. Please upload a ZIP file.",
                "supported_formats": [".zip"]
            }, status=400)
        
        # Create a job ID for tracking this upload
        job_id = str(uuid.uuid4())
        
        # Save ZIP to temporary location
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, f"bulk_upload_{job_id}.zip")
        
        with open(zip_path, 'wb') as f:
            for chunk in archive_file.chunks():
                f.write(chunk)
        
        # Process the ZIP file to extract mapping information
        mapping_results = process_zip_for_mapping(zip_path, exam_id)
        
        # Store results for confirmation step
        bulk_upload_jobs[job_id] = {
            "zip_path": zip_path,
            "exam_id": exam_id,
            "mappings": mapping_results["mappings"],
            "errors": mapping_results["errors"],
            "timestamp": datetime.now(),
            "status": "pending_confirmation"
        }
        
        # Return job ID and mapping information for confirmation
        return JsonResponse({
            "job_id": job_id,
            "message": "ZIP file processed successfully",
            "status": "pending_confirmation",
            "mappings": mapping_results["mappings"],
            "errors": mapping_results["errors"],
            "total_files": mapping_results["total_files"],
            "valid_files": len(mapping_results["mappings"]),
            "invalid_files": len(mapping_results["errors"]),
        }, status=200)
        
    except zipfile.BadZipFile:
        return JsonResponse({
            "error": "Invalid ZIP file format. The file could not be read as a ZIP archive.",
            "suggestion": "Please check that your file is a valid ZIP archive and not corrupted."
        }, status=400)
    except Exception as e:
        print(f"Error in bulk_upload_answer_sheets: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)

def process_zip_for_mapping(zip_path, exam_id):
    """
    Extract file mapping information from ZIP without uploading files.
    Returns mappings between filenames and student/subject information.
    """
    mappings = []
    errors = []
    total_files = 0

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = zip_ref.namelist()
            total_files = len(file_list)

            # Process each PDF file in the ZIP
            for filename in file_list:
                # Skip directories
                if filename.endswith('/'):
                    continue

                if not filename.lower().endswith('.pdf'):
                    errors.append({
                        "filename": filename,
                        "error": "Not a PDF file"
                    })
                    continue

                # Parse filename to extract register number and subject code
                # Expected format: register_number_subject_code.pdf or similar
                match = re.match(r'([a-zA-Z0-9]+)[-_]([a-zA-Z0-9]+).*\.pdf', os.path.basename(filename))

                if not match:
                    errors.append({
                        "filename": filename,
                        "error": "Filename does not match expected pattern (register_number_subject_code.pdf)"
                    })
                    continue

                register_number = match.group(1)
                subject_code = match.group(2)

                # Verify student exists
                student = student_collection.find_one({"register_number": register_number})
                if not student:
                    errors.append({
                        "filename": filename,
                        "error": f"Student with register number {register_number} not found"
                    })
                    continue

                # Verify exam has the subject
                exam = exam_collection.find_one({"_id": ObjectId(exam_id)})
                if not exam:
                    errors.append({
                        "filename": filename,
                        "error": f"Exam with ID {exam_id} not found"
                    })
                    continue

                subject_exists = False
                subject_name = ""
                for subject in exam.get("subjects", []):
                    if subject.get("subject_code") == subject_code:
                        subject_exists = True
                        subject_name = subject.get("subject_name", "")
                        break

                if not subject_exists:
                    errors.append({
                        "filename": filename,
                        "error": f"Subject code {subject_code} not found in exam {exam_id}"
                    })
                    continue

                # Add to valid mappings
                mappings.append({
                    "filename": filename,
                    "register_number": register_number,
                    "student_name": student.get("name", "Unknown"),
                    "subject_code": subject_code,
                    "subject_name": subject_name
                })

    except zipfile.BadZipFile:
        errors.append({
            "filename": os.path.basename(zip_path),
            "error": "Invalid ZIP file format"
        })
    except Exception as e:
        errors.append({
            "filename": os.path.basename(zip_path),
            "error": f"Error processing ZIP: {str(e)}"
        })

    return {
        "mappings": mappings,
        "errors": errors,
        "total_files": total_files
    }   

@csrf_exempt
def confirm_bulk_upload(request):
    """Confirm and finalize bulk upload by processing the uploaded files"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request method'}, status=405)
    
    try:
        data = json.loads(request.body)
        job_id = data.get('job_id')
        
        if not job_id:
            return JsonResponse({'error': 'Missing job ID'}, status=400)
        
        if job_id not in bulk_upload_jobs:
            return JsonResponse({'error': 'Invalid job ID'}, status=400)
        
        job = bulk_upload_jobs[job_id]
        
        # Check job status
        if job['status'] != 'pending_confirmation':
            return JsonResponse({'error': f"Job is in {job['status']} state, cannot confirm"}, status=400)
        
        confirmed_mappings = data.get('confirmed_mappings', [])
        
        # If no mappings confirmed, abort
        if not confirmed_mappings:
            bulk_upload_jobs[job_id]['status'] = 'aborted'
            # Clean up temp file
            try:
                os.remove(job['zip_path'])
            except:
                pass
            return JsonResponse({'message': 'Bulk upload aborted - no files confirmed'}, status=200)
        
        # Get exam ID and subject code
        exam_id = job['exam_id']
        subject_code = confirmed_mappings[0].get('subject_code') if confirmed_mappings else None
        
        # Attempt to save files to permanent storage
        uploaded_sheets = []
        
        with zipfile.ZipFile(job['zip_path'], 'r') as zip_ref:
            for mapping in confirmed_mappings:
                try:
                    filename = mapping['filename']
                    register_number = mapping['register_number']
                    subject_code = mapping['subject_code']
                    subject_name = mapping['subject_name']
                    
                    # Extract the file into a temporary file
                    temp_dir = tempfile.gettempdir()
                    temp_path = os.path.join(temp_dir, os.path.basename(filename))
                    
                    with zip_ref.open(filename) as source, open(temp_path, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    
                    # Upload to S3
                    s3_filename = f"answersheets/{exam_id}_{subject_code}_{register_number}_answersheet.pdf"
                    
                    with open(temp_path, 'rb') as file_data:
                        file_url = upload_to_s3(file_data, s3_filename)
                    
                    if not file_url:
                        raise Exception(f"Failed to upload {filename} to S3")
                    
                    # Create student data structure
                    student_data = {
                        "student_id": register_number,
                        "student_name": mapping['student_name'],
                        "file_url": file_url,
                        "file_name": os.path.basename(s3_filename),
                        "submitted_at": datetime.now(),
                        "has_answer_sheet": True,
                        "is_evaluated": False,
                        "status": "submitted"
                    }
                    
                    # Check if an answer sheet document already exists for this exam
                    existing_doc = answer_sheet_collection.find_one({"exam_id": exam_id})
                    answer_sheet_id = None
                    
                    if existing_doc:
                        # Document exists, check if the subject already exists
                        subject_exists = False
                        for subject in existing_doc.get('subjects', []):
                            if subject.get('subject_code') == subject_code:
                                subject_exists = True
                                
                                # Check if student already exists in this subject
                                student_exists = False
                                for student in subject.get('students', []):
                                    if student.get('student_id') == register_number:
                                        # Update existing student
                                        answer_sheet_collection.update_one(
                                            {
                                                "exam_id": exam_id,
                                                "subjects.subject_code": subject_code,
                                                "subjects.students.student_id": register_number
                                            },
                                            {
                                                "$set": {
                                                    "subjects.$[subj].students.$[stu].file_url": file_url,
                                                    "subjects.$[subj].students.$[stu].submitted_at": datetime.now(),
                                                    "subjects.$[subj].students.$[stu].has_answer_sheet": True,
                                                    "subjects.$[subj].students.$[stu].is_evaluated": False,
                                                    "subjects.$[subj].students.$[stu].status": "submitted",
                                                    "subjects.$[subj].students.$[stu].file_name": os.path.basename(s3_filename)
                                                }
                                            },
                                            array_filters=[
                                                {"subj.subject_code": subject_code},
                                                {"stu.student_id": register_number}
                                            ]
                                        )
                                        student_exists = True
                                        break
                                
                                if not student_exists:
                                    # Add new student to existing subject
                                    answer_sheet_collection.update_one(
                                        {
                                            "exam_id": exam_id,
                                            "subjects.subject_code": subject_code
                                        },
                                        {
                                            "$push": {
                                                "subjects.$.students": {
                                                    **student_data,
                                                    "file_name": os.path.basename(s3_filename)
                                                }
                                            }
                                        }
                                    )
                                break
                        
                        if not subject_exists:
                            # Add new subject with this student
                            answer_sheet_collection.update_one(
                                {"exam_id": exam_id},
                                {
                                    "$push": {
                                        "subjects": {
                                            "subject_code": subject_code,
                                            "subject_name": subject_name,
                                            "students": [
                                                {
                                                    **student_data,
                                                    "file_name": os.path.basename(s3_filename)
                                                }
                                            ]
                                        }
                                    }
                                }
                            )
                        
                        answer_sheet_id = str(existing_doc['_id'])
                    else:
                        # Create new document with nested structure
                        answer_sheet_doc = {
                            "exam_id": exam_id,
                            "subjects": [
                                {
                                    "subject_code": subject_code,
                                    "subject_name": subject_name,
                                    "students": [
                                        {
                                            **student_data,
                                            "file_name": os.path.basename(s3_filename)
                                        }
                                    ]
                                }
                            ]
                        }
                        result = answer_sheet_collection.insert_one(answer_sheet_doc)
                        answer_sheet_id = str(result.inserted_id)
                    
                    # Add to uploaded sheets for exam update
                    uploaded_sheets.append({
                        "answer_sheet_id": answer_sheet_id,
                        "student_id": register_number,
                        "student_name": mapping['student_name'],
                        "subject_code": subject_code,
                        "submitted_at": datetime.now()
                    })
                    
                    # Clean up temp file
                    try:
                        os.remove(temp_path)
                    except Exception as cleanup_error:
                        print(f"⚠️ Could not remove temporary file {temp_path}: {str(cleanup_error)}")
                    
                    # Update the mapping with success info
                    mapping['success'] = True
                    mapping['answer_sheet_id'] = answer_sheet_id
                    mapping['file_url'] = file_url
                    
                except Exception as e:
                    print(f"❌ Failed to process file {mapping.get('filename')}: {str(e)}")
                    mapping['success'] = False
                    mapping['error'] = str(e)
        
        # After all uploads are completed, update the exam document with the answer sheets
        if uploaded_sheets:
            try:
                # Get the exam document
                exam = exam_collection.find_one({"_id": ObjectId(exam_id)})
                
                if not exam:
                    print(f"❌ Exam not found with ID: {exam_id}")
                else:
                    # Initialize the answer_sheets array if it doesn't exist
                    if "answer_sheets" not in exam:
                        exam_collection.update_one(
                            {"_id": ObjectId(exam_id)},
                            {"$set": {"answer_sheets": []}}
                        )
                        exam = exam_collection.find_one({"_id": ObjectId(exam_id)})
                    
                    # Get current answer sheets
                    answer_sheets = exam.get("answer_sheets", [])
                    
                    # Create a set of existing student/subject combinations
                    existing_entries = set()
                    for sheet in answer_sheets:
                        key = f"{sheet.get('student_id')}_{sheet.get('subject_code')}"
                        existing_entries.add(key)
                    
                    # Add only non-duplicate entries
                    new_sheets = []
                    for sheet in uploaded_sheets:
                        key = f"{sheet.get('student_id')}_{sheet.get('subject_code')}"
                        if key not in existing_entries:
                            new_sheets.append(sheet)
                            existing_entries.add(key)
                    
                    # Update the exam with the new sheets if any
                    if new_sheets:
                        exam_collection.update_one(
                            {"_id": ObjectId(exam_id)},
                            {"$push": {"answer_sheets": {"$each": new_sheets}}}
                        )
                        print(f"✅ Updated exam {exam_id} with {len(new_sheets)} new answer sheets")
            except Exception as e:
                print(f"❌ Error updating exam with answer sheets: {str(e)}")
        
        # Clean up the temporary zip file
        try:
            os.remove(job['zip_path'])
            print(f"✅ Removed temporary file: {job['zip_path']}")
        except Exception as cleanup_error:
            print(f"⚠️ Could not remove temporary file {job['zip_path']}: {str(cleanup_error)}")
        
        # Update job status
        job['status'] = 'completed'
        job['confirmed_mappings'] = confirmed_mappings
        
        return JsonResponse({
            'success': True,
            'message': 'Upload completed successfully',
            'mappings': confirmed_mappings
        })
        
    except Exception as e:
        print(f"❌ Error in confirm_bulk_upload: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)
        
def update_exam_with_answer_sheets_after_upload(exam_id, uploaded_sheets):
    """
    Updates the exam document to include all newly uploaded answer sheets.
    This function ensures all students from the answer sheet document are reflected in the exam collection.
    
    Args:
        exam_id (str): The ID of the exam to update
        uploaded_sheets (list): List of successfully uploaded answer sheet data
    
    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        # Get the exam document
        exam_collection = db['exam_details']
        
        # Convert to ObjectId for MongoDB query
        try:
            exam_object_id = ObjectId(exam_id)
        except:
            print(f"❌ Invalid exam ID format: {exam_id}")
            return False
            
        # Get the exam document
        exam = exam_collection.find_one({"_id": exam_object_id})
        if not exam:
            print(f"❌ Exam not found with ID: {exam_id}")
            return False
            
        # Initialize the answer_sheets array if it doesn't exist
        if "answer_sheets" not in exam:
            exam["answer_sheets"] = []
            
        # Get existing answer sheets and answer sheet IDs
        answer_sheets = exam.get("answer_sheets", [])
        existing_ids = [sheet.get("answer_sheet_id") for sheet in answer_sheets]
        
        # Track how many sheets were added
        sheets_added = 0
        
        # Get all students from the answer sheet document in MongoDB
        # Instead of relying on the uploaded_sheets list, which might be incomplete
        answer_sheet_docs = answer_sheet_collection.find({"exam_id": exam_id})
        
        for answer_sheet_doc in answer_sheet_docs:
            answer_sheet_id = str(answer_sheet_doc["_id"])
            
            # Skip if this answer sheet ID is already in the exam document
            if answer_sheet_id in existing_ids:
                continue
                
            # Process all students in all subjects
            for subject in answer_sheet_doc.get("subjects", []):
                subject_code = subject.get("subject_code")
                
                for student in subject.get("students", []):
                    student_id = student.get("student_id")
                    student_name = student.get("student_name")
                    submitted_at = student.get("submitted_at", datetime.now())
                    
                    # Create new answer sheet entry
                    answer_sheet_entry = {
                        "answer_sheet_id": answer_sheet_id,
                        "student_id": student_id,
                        "student_name": student_name,
                        "subject_code": subject_code,
                        "submitted_at": submitted_at
                    }
                    
                    # Check if an entry for this student and subject already exists
                    entry_exists = False
                    for sheet in answer_sheets:
                        if (sheet.get("student_id") == student_id and 
                            sheet.get("subject_code") == subject_code):
                            entry_exists = True
                            break
                    
                    # Add to the array if it doesn't exist
                    if not entry_exists:
                        answer_sheets.append(answer_sheet_entry)
                        sheets_added += 1
        
        # Update the exam document if we added any sheets
        if sheets_added > 0:
            result = exam_collection.update_one(
                {"_id": exam_object_id},
                {"$set": {"answer_sheets": answer_sheets}}
            )
            
            print(f"✅ Updated exam {exam_id} with {sheets_added} new answer sheets")
            return True
        else:
            print(f"ℹ️ No new answer sheets to add to exam {exam_id}")
            return True
            
    except Exception as e:
        print(f"❌ Error updating exam with answer sheets: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return False
    
def process_bulk_upload(job_id):
    """
    Process the confirmed files in the ZIP archive and upload to S3.
    This processes files sequentially to avoid API rate limits.
    """
    if job_id not in bulk_upload_jobs:
        print(f"❌ Job {job_id} not found in bulk_upload_jobs")
        return
    
    job = bulk_upload_jobs[job_id]
    zip_path = job["zip_path"]
    exam_id = job["exam_id"]
    confirmed_mappings = job["confirmed_mappings"]
    
    # Initialize tracking for uploaded sheets
    uploaded_sheets = []
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Process each file sequentially to avoid overwhelming the API
            for mapping in confirmed_mappings:
                try:
                    # Process one file at a time
                    result = upload_single_file(zip_ref, mapping, exam_id)
                    
                    if result["success"]:
                        job["results"]["successful"].append(result)
                        uploaded_sheets.append(result)
                    else:
                        job["results"]["failed"].append(result)
                        
                except Exception as e:
                    print(f"❌ Error processing file {mapping['filename']}: {str(e)}")
                    job["results"]["failed"].append({
                        "mapping": mapping,
                        "success": False,
                        "error": str(e)
                    })
                
                # Update progress count
                job["results"]["completed"] += 1
                
                # Add a delay between uploads to avoid rate limiting
                time.sleep(2)
        
        # After all uploads are completed, update the exam document with the answer sheets
        if uploaded_sheets:
            update_result = update_exam_with_answer_sheets_after_upload(exam_id, uploaded_sheets)
            if update_result:
                print(f"✅ Successfully updated exam {exam_id} with {len(uploaded_sheets)} answer sheets")
            else:
                print(f"❌ Failed to update exam {exam_id} with answer sheets")
                job["warning"] = "Answer sheets were uploaded but could not be linked to the exam"
        
        # Update job status
        job["status"] = "completed"
    
    except Exception as e:
        print(f"❌ Bulk upload processing failed: {str(e)}")
        job["status"] = "failed"
        job["error"] = str(e)
    
    # Clean up temp file
    try:
        os.remove(zip_path)
        print(f"✅ Removed temporary file: {zip_path}")
    except Exception as cleanup_error:
        print(f"⚠️ Could not remove temporary file {zip_path}: {str(cleanup_error)}")
        
                
        
def upload_single_file(zip_ref, mapping, exam_id):
    """
    Extract and upload a single file from the ZIP archive to S3.
    Stores answer sheets with a nested structure matching your query pattern.
    Returns result of the operation.
    """
    filename = mapping["filename"]
    register_number = mapping["register_number"]
    subject_code = mapping["subject_code"]
    
    try:
        # Extract the file into a temporary file
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, os.path.basename(filename))
        
        with zip_ref.open(filename) as source, open(temp_path, 'wb') as target:
            target.write(source.read())
        
        # Upload to S3
        s3_filename = f"answersheets/{exam_id}_{subject_code}_{register_number}_answersheet.pdf"
        
        with open(temp_path, 'rb') as file_data:
            file_url = upload_to_s3(file_data, s3_filename)
        
        if not file_url:
            raise Exception(f"Failed to upload {filename} to S3")
        
        # Create the student document structure
        student_data = {
            "student_id": register_number,
            "student_name": mapping["student_name"],
            "file_url": file_url,
            "submitted_at": datetime.now(),
            "has_answer_sheet": True,
            "is_evaluated": False,
            "status": "submitted"
        }
        
        # Check if answer sheet document already exists for this exam
        existing_doc = answer_sheet_collection.find_one({
            "exam_id": exam_id
        })
        
        if existing_doc:
            # Document exists, check if the subject already exists
            subject_exists = False
            for subject in existing_doc.get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    subject_exists = True
                    
                    # Check if student already exists in this subject
                    student_exists = False
                    for student in subject.get("students", []):
                        if student.get("student_id") == register_number:
                            # Update existing student
                            answer_sheet_collection.update_one(
                                {
                                    "exam_id": exam_id,
                                    "subjects.subject_code": subject_code,
                                    "subjects.students.student_id": register_number
                                },
                                {
                                    "$set": {
                                        "subjects.$[subj].students.$[stu].file_url": file_url,
                                        "subjects.$[subj].students.$[stu].submitted_at": datetime.now(),
                                        "subjects.$[subj].students.$[stu].has_answer_sheet": True,
                                        "subjects.$[subj].students.$[stu].is_evaluated": False,
                                        "subjects.$[subj].students.$[stu].status": "submitted",
                                        "subjects.$[subj].students.$[stu].file_name": os.path.basename(s3_filename)
                                    }
                                },
                                array_filters=[
                                    {"subj.subject_code": subject_code},
                                    {"stu.student_id": register_number}
                                ]
                            )
                            student_exists = True
                            break
                    
                    if not student_exists:
                        # Add new student to existing subject
                        answer_sheet_collection.update_one(
                            {
                                "exam_id": exam_id,
                                "subjects.subject_code": subject_code
                            },
                            {
                                "$push": {
                                    "subjects.$.students": {
                                        **student_data,
                                        "file_name": os.path.basename(s3_filename)
                                    }
                                }
                            }
                        )
                    break
            
            if not subject_exists:
                # Add new subject with this student
                answer_sheet_collection.update_one(
                    {"exam_id": exam_id},
                    {
                        "$push": {
                            "subjects": {
                                "subject_code": subject_code,
                                "subject_name": mapping["subject_name"],
                                "students": [
                                    {
                                        **student_data,
                                        "file_name": os.path.basename(s3_filename)
                                    }
                                ]
                            }
                        }
                    }
                )
        else:
            # Create new document with nested structure
            answer_sheet_doc = {
                "exam_id": exam_id,
                "subjects": [
                    {
                        "subject_code": subject_code,
                        "subject_name": mapping["subject_name"],
                        "students": [
                            {
                                **student_data,
                                "file_name": os.path.basename(s3_filename)
                            }
                        ]
                    }
                ]
            }
            result = answer_sheet_collection.insert_one(answer_sheet_doc)
        
        # Get the ID of the answer sheet document for tracking
        answer_sheet_doc = answer_sheet_collection.find_one({
            "exam_id": exam_id,
            "subjects": {
                "$elemMatch": {
                    "subject_code": subject_code,
                    "students": {
                        "$elemMatch": {
                            "student_id": register_number
                        }
                    }
                }
            }
        })
        answer_sheet_id = str(answer_sheet_doc["_id"]) if answer_sheet_doc else "unknown"
        
        # Clean up temp file
        try:
            os.remove(temp_path)
        except Exception as cleanup_error:
            print(f"⚠️ Could not remove temporary file {temp_path}: {str(cleanup_error)}")
        
        return {
            "mapping": mapping,
            "success": True,
            "answer_sheet_id": answer_sheet_id,
            "file_url": file_url
        }
    
    except Exception as e:
        # Clean up temp file if it exists
        try:
            if 'temp_path' in locals():
                os.remove(temp_path)
        except:
            pass
        
        print(f"❌ Failed to upload file {filename}: {str(e)}")
        return {
            "mapping": mapping,
            "success": False,
            "error": str(e)
        }
                
        
@csrf_exempt
def get_bulk_upload_status(request, job_id):
    """
    Check the status of a bulk upload job
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        if job_id not in bulk_upload_jobs:
            return JsonResponse({"error": "Job not found or expired"}, status=404)
        
        job = bulk_upload_jobs[job_id]
        
        # Prepare response based on job status
        response = {
            "job_id": job_id,
            "status": job["status"],
            "exam_id": job["exam_id"],
            "timestamp": job["timestamp"].isoformat(),
        }
        
        # Include appropriate details based on status
        if job["status"] == "pending_confirmation":
            response["mappings"] = job["mappings"]
            response["errors"] = job["errors"]
            response["total_files"] = len(job["mappings"]) + len(job["errors"])
            response["valid_files"] = len(job["mappings"])
            response["invalid_files"] = len(job["errors"])
            
        elif job["status"] in ["uploading", "completed", "failed"]:
            if "results" in job:
                response["progress"] = {
                    "total": job["results"]["total"],
                    "completed": job["results"]["completed"],
                    "successful": len(job["results"]["successful"]),
                    "failed": len(job["results"]["failed"]),
                    "percent_complete": round(job["results"]["completed"] / job["results"]["total"] * 100, 1) if job["results"]["total"] > 0 else 0
                }
            
            # If status is failed, include the error
            if job["status"] == "failed" and "error" in job:
                response["error"] = job["error"]
        
        return JsonResponse(response, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

# @csrf_exempt
# def start_bulk_evaluation(request):
#     """
#     Start evaluation of multiple answer sheets in parallel
#     """
#     if request.method != "POST":
#         return JsonResponse({"error": "Invalid request method"}, status=405)
    
#     try:
#         data = json.loads(request.body)
#         exam_id = data.get("exam_id")
#         subject_code = data.get("subject_code")
#         answer_sheet_ids = data.get("answer_sheet_ids", [])
        
#         # If answer_sheet_ids is empty, find all unevaluated answer sheets for the exam/subject
#         if not answer_sheet_ids and exam_id and subject_code:
#             answer_sheets = answer_sheet_collection.find({
#                 "exam_id": exam_id,
#                 "subject_code": subject_code,
#                 "$or": [
#                     {"is_evaluated": {"$exists": False}},
#                     {"is_evaluated": False}
#                 ]
#             })
            
#             answer_sheet_ids = [str(sheet["_id"]) for sheet in answer_sheets]
        
#         if not answer_sheet_ids:
#             return JsonResponse({"error": "No answer sheets to evaluate"}, status=400)
        
#         if not exam_id:
#             return JsonResponse({"error": "Missing exam_id parameter"}, status=400)
        
#         # Create a job ID for tracking this evaluation
#         job_id = str(uuid.uuid4())
        
#         # Initialize job tracking
#         evaluation_jobs[job_id] = {
#             "exam_id": exam_id,
#             "subject_code": subject_code,
#             "answer_sheet_ids": answer_sheet_ids,
#             "status": "processing",
#             "timestamp": datetime.now(),
#             "results": {
#                 "total": len(answer_sheet_ids),
#                 "completed": 0,
#                 "successful": [],
#                 "failed": []
#             }
#         }
        
#         # Start evaluation process in background
#         eval_thread = threading.Thread(
#             target=process_bulk_evaluation,
#             args=(job_id,)
#         )
#         eval_thread.daemon = True
#         eval_thread.start()
        
#         # Return immediate response
#         return JsonResponse({
#             "job_id": job_id,
#             "message": "Evaluation processing started",
#             "status": "processing",
#             "sheets_to_process": len(answer_sheet_ids)
#         }, status=200)
        
#     except Exception as e:
#         return JsonResponse({"error": str(e)}, status=500)

# def process_bulk_evaluation(job_id):
#     """
#     Process evaluation of multiple answer sheets with strict sequential processing
#     and proper delays between API calls to avoid rate limiting
#     """
#     if job_id not in evaluation_jobs:
#         return
    
#     job = evaluation_jobs[job_id]
#     answer_sheet_ids = job["answer_sheet_ids"]
    
#     # Add tracking for API usage to monitor rate limits
#     job["api_usage"] = {
#         "total_calls": 0,
#         "successful_calls": 0,
#         "rate_limited_calls": 0,
#         "last_call_time": None
#     }
    
#     try:
#         # Process each answer sheet one at a time with proper delays
#         for i, sheet_id in enumerate(answer_sheet_ids):
#             try:
#                 print(f"Processing answer sheet {i+1}/{len(answer_sheet_ids)}: {sheet_id}")
                
#                 # Enforce a minimum delay between evaluations to avoid rate limits
#                 # First sheet doesn't need a delay
#                 if i > 0:
#                     # Calculate minimum delay - at least 6 seconds between each paper
#                     # which should keep us well under the 15 requests/minute limit
#                     delay = 6
#                     print(f"Waiting {delay} seconds before processing next answer sheet...")
#                     time.sleep(delay)
                
#                 # Record API call start time
#                 job["api_usage"]["last_call_time"] = datetime.now().isoformat()
#                 job["api_usage"]["total_calls"] += 1
                
#                 # Evaluate one sheet at a time
#                 result = evaluate_single_sheet(sheet_id)
                
#                 if result["success"]:
#                     job["results"]["successful"].append(result)
#                     job["api_usage"]["successful_calls"] += 1
#                 else:
#                     job["results"]["failed"].append(result)
#                     # Check if failure was due to rate limiting
#                     if "rate limit" in str(result.get("error", "")).lower() or "quota" in str(result.get("error", "")).lower():
#                         job["api_usage"]["rate_limited_calls"] += 1
#                         print(f"⚠️ Rate limit hit, adding extra delay")
#                         time.sleep(15)  # Add substantial delay after rate limit
                    
#             except Exception as e:
#                 error_msg = str(e)
#                 job["results"]["failed"].append({
#                     "answer_sheet_id": sheet_id,
#                     "success": False,
#                     "error": error_msg
#                 })
                
#                 # Check if exception was related to rate limiting
#                 if "rate limit" in error_msg.lower() or "quota" in error_msg.lower():
#                     job["api_usage"]["rate_limited_calls"] += 1
#                     print(f"⚠️ Rate limit exception: {error_msg}")
#                     # Add substantial delay after rate limit exception
#                     time.sleep(15)
            
#             # Update progress count
#             job["results"]["completed"] += 1
            
#             # Add a status update log
#             print(f"Progress: {job['results']['completed']}/{job['results']['total']} sheets processed")
            
#         # Update job status
#         job["status"] = "completed"
#         print(f"✅ Job {job_id} completed: {len(job['results']['successful'])} successful, {len(job['results']['failed'])} failed")
    
#     except Exception as e:
#         job["status"] = "failed"
#         job["error"] = str(e)
#         print(f"❌ Job {job_id} failed: {str(e)}")              
def evaluate_single_sheet(answer_sheet_id):
    """
    Evaluate a single answer sheet with improved error handling and rate limit detection.
    Forces re-evaluation for all sheets to ensure consistent processing.
    """
    try:
        # Find the answer sheet
        answer_sheet = answer_sheet_collection.find_one({"_id": ObjectId(answer_sheet_id)})
        if not answer_sheet:
            raise Exception(f"Answer sheet not found: {answer_sheet_id}")
        
        # Extract required information from the nested structure
        exam_id = answer_sheet.get("exam_id")
        
        # We need to extract subject_code and register_number from the nested structure
        # Handle both flat and nested document structures
        if "student_id" in answer_sheet and "subject_code" in answer_sheet:
            # Flat structure
            subject_code = answer_sheet.get("subject_code")
            register_number = answer_sheet.get("student_id")
            file_url = answer_sheet.get("file_url")
            student_name = answer_sheet.get("student_name")
            
            # Create a payload for the evaluation function
            payload = {
                "exam_id": exam_id,
                "register_number": register_number,
                "answer_sheet_id": answer_sheet_id,
                "subject_code": subject_code
            }
            
            # Log to make it clear we're processing this sheet
            print(f"Processing evaluation for sheet {answer_sheet_id} - student: {register_number}, subject: {subject_code}")
            
            # Call the evaluation function with a mock request
            from io import BytesIO
            import json
            
            class MockRequest:
                def __init__(self, payload):
                    self.body = json.dumps(payload).encode('utf-8')
                    self.method = "POST"
            
            mock_request = MockRequest(payload)
            evaluation_response = process_and_evaluate_answer_sheet(mock_request)
            
            # Parse the response
            response_data = json.loads(evaluation_response.content)
            
            # Handle errors in the response
            if "error" in response_data:
                error_msg = response_data["error"]
                print(f"❌ Evaluation error: {error_msg}")
                
                # Check specifically for API rate limit errors
                if "rate limit" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
                    print(f"⚠️ API rate limit detected for sheet {answer_sheet_id}")
                    return {
                        "answer_sheet_id": answer_sheet_id,
                        "success": False,
                        "error": "API rate limit exceeded",
                        "rate_limited": True
                    }
                
                return {
                    "answer_sheet_id": answer_sheet_id,
                    "success": False,
                    "error": error_msg
                }
            
            # Success case
            total_marks = response_data.get("total_marks", 0)
            print(f"✅ Successfully evaluated sheet {answer_sheet_id} - marks: {total_marks}")
            
            return {
                "answer_sheet_id": answer_sheet_id,
                "success": True,
                "total_marks": total_marks,
                "extracted_answers": response_data.get("extracted_answers", 0),
                "evaluation_id": response_data.get("evaluation_id", "")
            }
            
        else:
            # Nested structure - process all students in all subjects
            results = []
            
            for subject_idx, subject in enumerate(answer_sheet.get("subjects", [])):
                subject_code = subject.get("subject_code")
                subject_name = subject.get("subject_name", "")
                
                for student_idx, student in enumerate(subject.get("students", [])):
                    register_number = student.get("student_id")
                    file_url = student.get("file_url")
                    student_name = student.get("student_name", "")
                    
                    # Skip if already evaluated
                    if student.get("is_evaluated", False):
                        print(f"Skipping already evaluated student {register_number} in subject {subject_code}")
                        continue
                    
                    # Create a payload for the evaluation function
                    payload = {
                        "exam_id": exam_id,
                        "register_number": register_number,
                        "answer_sheet_id": answer_sheet_id,
                        "subject_code": subject_code
                    }
                    
                    # Log to make it clear we're processing this student
                    print(f"Processing evaluation for sheet {answer_sheet_id} - student: {register_number}, subject: {subject_code}")
                    
                    # Call the evaluation function with a mock request
                    from io import BytesIO
                    import json
                    
                    class MockRequest:
                        def __init__(self, payload):
                            self.body = json.dumps(payload).encode('utf-8')
                            self.method = "POST"
                    
                    mock_request = MockRequest(payload)
                    evaluation_response = process_and_evaluate_answer_sheet(mock_request)
                    
                    # Parse the response
                    response_data = json.loads(evaluation_response.content)
                    
                    # Handle errors in the response
                    if "error" in response_data:
                        error_msg = response_data["error"]
                        print(f"❌ Evaluation error for student {register_number}: {error_msg}")
                        
                        # Check specifically for API rate limit errors
                        if "rate limit" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
                            print(f"⚠️ API rate limit detected for student {register_number}")
                            results.append({
                                "answer_sheet_id": answer_sheet_id,
                                "student_id": register_number,
                                "subject_code": subject_code,
                                "success": False,
                                "error": "API rate limit exceeded",
                                "rate_limited": True
                            })
                            
                            # If we hit a rate limit, return immediately to prevent further API calls
                            return {
                                "answer_sheet_id": answer_sheet_id,
                                "success": False,
                                "error": "API rate limit exceeded",
                                "rate_limited": True,
                                "results": results
                            }
                        
                        results.append({
                            "answer_sheet_id": answer_sheet_id,
                            "student_id": register_number,
                            "subject_code": subject_code,
                            "success": False,
                            "error": error_msg
                        })
                        continue
                    
                    # Success case
                    total_marks = response_data.get("total_marks", 0)
                    print(f"✅ Successfully evaluated student {register_number} - marks: {total_marks}")
                    
                    results.append({
                        "answer_sheet_id": answer_sheet_id,
                        "student_id": register_number,
                        "subject_code": subject_code,
                        "success": True,
                        "total_marks": total_marks,
                        "extracted_answers": response_data.get("extracted_answers", 0),
                        "evaluation_id": response_data.get("evaluation_id", "")
                    })
            
            # Determine overall success based on individual results
            if not results:
                return {
                    "answer_sheet_id": answer_sheet_id,
                    "success": False,
                    "error": "No students found to evaluate"
                }
            
            # Check if any student was evaluated successfully
            any_success = any(result["success"] for result in results)
            
            # Check if any student hit a rate limit
            any_rate_limited = any(result.get("rate_limited", False) for result in results)
            
            if any_success:
                return {
                    "answer_sheet_id": answer_sheet_id,
                    "success": True,
                    "results": results,
                    "rate_limited": any_rate_limited
                }
            else:
                # All students failed
                return {
                    "answer_sheet_id": answer_sheet_id,
                    "success": False,
                    "error": "All student evaluations failed",
                    "results": results,
                    "rate_limited": any_rate_limited
                }
    
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error evaluating sheet {answer_sheet_id}: {error_msg}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Check if the error is related to rate limiting
        if "rate limit" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
            return {
                "answer_sheet_id": answer_sheet_id,
                "success": False,
                "error": f"API rate limit exceeded: {error_msg}",
                "rate_limited": True
            }
        
        return {
            "answer_sheet_id": answer_sheet_id,
            "success": False,
            "error": error_msg
        }
                                                
@csrf_exempt
def get_evaluation_status(request, job_id):
    """
    Check the status of a bulk evaluation job
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    try:
        if job_id not in evaluation_jobs:
            return JsonResponse({"error": "Job not found or expired"}, status=404)
        
        job = evaluation_jobs[job_id]
        
        # Prepare response
        response = {
            "job_id": job_id,
            "status": job["status"],
            "exam_id": job["exam_id"],
            "subject_code": job["subject_code"],
            "timestamp": job["timestamp"].isoformat(),
            "progress": {
                "total": job["results"]["total"],
                "completed": job["results"]["completed"],
                "successful": len(job["results"]["successful"]),
                "failed": len(job["results"]["failed"]),
                "percent_complete": round(job["results"]["completed"] / job["results"]["total"] * 100, 1) if job["results"]["total"] > 0 else 0
            }
        }
        
        # If status is failed, include the error
        if job["status"] == "failed" and "error" in job:
            response["error"] = job["error"]
        
        return JsonResponse(response, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
    
@csrf_exempt
def search_or_create_exam(request):
    """
    Search for an exam and create it if not found

    Request body should contain the following fields:
        - examType (string)
        - batch (string)
        - department (string)
        - year (string)
        - semester (string)
        - section (string)
    
    Returns a JSON response with the created/updated exam details if successful
    
    :param request: The HTTP request object
    :return: A JSON response with the created/updated exam details if successful
    """
    try:
        # Check request method
        if request.method != "POST":
            return JsonResponse({'error': 'Only POST method allowed'}, status=405)

        # Check authorization
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

        token = auth_header.split(' ')[1]
        decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        creator_name = decoded_token.get('name')
        creator_id = decoded_token.get('admin_user')

        # Parse JSON body
        if request.content_type and "application/json" in request.content_type:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({'error': 'Invalid JSON format'}, status=400)
        else:
            return JsonResponse({'error': 'Content-Type must be application/json'}, status=400)

        # Required fields for query
        query = {
            "exam_type": data.get("examType", ""),
            "batch": data.get("batch", ""),
            "department": data.get("department", ""),
            "year": data.get("year", ""),
            "semester": data.get("semester", ""),
        }

        # Add creator info to query
        if creator_name: query["created_by.name"] = creator_name
        if creator_id: query["created_by.id"] = creator_id

        for_sem_query ={
            "batch": query["batch"],
            "department": query["department"],
            "year": query["year"],
            "semester": query["semester"]
        }

        

        # Search exam
        exam = exam_collection.find_one(query)
    

        # Create if not found
        if not exam:
            exam_data = {
                "exam_type": query["exam_type"],
                "batch": query["batch"],
                "department": query["department"],
                "year": query["year"],
                "semester": query["semester"],
                "section": data.get("section", ""),
                "created_by": {
                    "name": creator_name,
                    "id": creator_id
                },
                "created_at": datetime.now()
            }
            inserted = exam_collection.insert_one(exam_data)
            exam = exam_collection.find_one({"_id": inserted.inserted_id})

        # Clean response
        cleaned_exam = {
            "_id": str(exam["_id"]),
            "exam_type": exam.get("exam_type", ""),
            "batch": exam.get("batch", ""),
            "department": exam.get("department", ""),
            "year": exam.get("year", ""),
            "semester": exam.get("semester", ""),
            "section": exam.get("section", ""),
            "created_by": exam.get("created_by", {}),
            "created_at": exam.get("created_at").isoformat() if exam.get("created_at") else None
        }

        # Add subjects if any
        if "subjects" in exam:
            cleaned_exam["subjects"] = [
                {
                    "subject_name": sub.get("subject_name", ""),
                    "subject_code": sub.get("subject_code", ""),
                    "question_paper": {
                        "filename": sub.get("question_paper", {}).get("filename", ""),
                        "url": sub.get("question_paper", {}).get("url", "")
                    },
                    "answer_key": {
                        "filename": sub.get("answer_key", {}).get("filename", ""),
                        "url": sub.get("answer_key", {}).get("url", "")
                    }
                }
                for sub in exam["subjects"]
            ]


        print(f"Searching for search_or_create_exam for_sem_query with query: {for_sem_query}")

        # Add semester details
        sem = semester_collection.find_one(for_sem_query)

        if not sem:
            sem_data = {
                "batch": query["batch"],
                "department": query["department"],
                "year": query["year"],
                "semester": query["semester"],
                "created_by": {
                    "name": creator_name,
                    "id": creator_id
                },
                "created_at": datetime.now()
            }
            inserted = semester_collection.insert_one(sem_data)
            save_subjects_to_sem_exam(inserted.inserted_id)
        else:
            save_subjects_to_sem_exam(sem["_id"])

        return JsonResponse({"exam": cleaned_exam}, status=200)

    except jwt.InvalidTokenError:
        return JsonResponse({'error': 'Invalid token'}, status=401)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    



@csrf_exempt
def submit_semester_details(request):

    """
    Submit semester details. If the semester does not exist, create it.
    
    Request body should contain the following fields:
        - batch (string)
        - department (string)
        - year (string)
        - semester (string)
        
    Returns a JSON response with the created/updated semester details if successful.
    
    :param request: The HTTP request object
    :return: A JSON response with the created/updated semester details if successful
    """
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)
    
    # Check authorization
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return JsonResponse({'error': 'Authorization token is missing or invalid'}, status=401)

    token = auth_header.split(' ')[1]
    decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    creator_name = decoded_token.get('name')
    creator_id = decoded_token.get('admin_user')

    
    try:
        data = json.loads(request.body)
        # Required fields for exam search/creation
        batch = data.get("batch", "")
        department = data.get("department", "")
        year = data.get("year", "")
        semester = data.get("semester", "")

        if not all([ batch, department, year, semester]):
            return JsonResponse({"error": "Missing required exam details"}, status=400)

        # Add creator info from JWT
        creator_name = decoded_token.get('name')
        creator_id = decoded_token.get('admin_user')

        query = {
            "batch": batch,
            "department": department,
            "year": year,
            "semester": semester,
            "subjects": {"$exists": True}  # Ensure subjects exist
        }
        if creator_name: query["created_by.name"] = creator_name
        if creator_id: query["created_by.id"] = creator_id

        print(f"Searching for submit_semester_details for_sem_query with query: {query}")

        # Search for existing Sem
        sem = semester_collection.find_one(query)
        if not sem:
            sem_data = {
                "batch": batch,
                "department": department,
                "year": year,
                "semester": semester,
                "created_by": {
                    "name": creator_name,
                    "id": creator_id
                },
                "created_at": datetime.now()
            }
            inserted = semester_collection.insert_one(sem_data)
            sem = semester_collection.find_one({"_id": inserted.inserted_id})

        cleaned_sem = {
            "_id": str(sem["_id"]),
            "batch": sem.get("batch", ""),
            "department": sem.get("department", ""),
            "year": sem.get("year", ""),
            "semester": sem.get("semester", ""),
            "created_by": sem.get("created_by", {}),
            "created_at": sem.get("created_at").isoformat() if sem.get("created_at") else None
        }
        if "subjects" in sem:
            cleaned_sem["subjects"] = [
                {
                    "subject_name": sub.get("subject_name", ""),
                    "subject_code": sub.get("subject_code", "")
                }
                for sub in sem["subjects"]
            ]

        return JsonResponse({"sem": cleaned_sem}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
    
# Set up logging
logger = logging.getLogger(__name__)


@csrf_exempt
def calculate_question_averages(request):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        # Parse JSON payload
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("Invalid JSON payload received")
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        # Get subject_code and exam_id from payload
        subject_code = data.get('subject_code', '').strip()
        exam_id = data.get('exam_id', '').strip()

        # Validate input
        if not subject_code:
            logger.warning("Missing subject_code in request")
            return JsonResponse({"error": "subject_code is required"}, status=400)

        if not exam_id:
            logger.warning("Missing exam_id in request")
            return JsonResponse({"error": "exam_id is required"}, status=400)

        # MongoDB connection (assumes db is configured)
        results_collection = db['results']

        # Query with optimized projection to fetch only required fields
        result = results_collection.find_one(
            {
                "exam_id": exam_id,
                "results.subjects.subject_code": subject_code
            },
            projection={
                "results.subjects.$": 1  # Fetch only the matching subject
            }
        )

        if not result:
            logger.info(f"No results found for subject_code: {subject_code}, exam_id: {exam_id}")
            return JsonResponse({"error": "No results found for the given subject_code and exam_id"}, status=404)

        # Extract the relevant subject data
        subject_data = next(
            (subject for subject in result['results']['subjects']
             if subject['subject_code'] == subject_code),
            None
        )

        if not subject_data:
            logger.error(f"Subject not found in results for subject_code: {subject_code}, exam_id: {exam_id}")
            return JsonResponse({"error": "Subject not found in results"}, status=404)

        # Initialize dictionaries for sums and counts
        question_sums = {}
        question_counts = {}
        student_count = len(subject_data['students'])

        if student_count == 0:
            logger.info(f"No students found for subject_code: {subject_code}, exam_id: {exam_id}")
            return JsonResponse({
                "subject_code": subject_code,
                "exam_id": exam_id,
                "student_count": 0,
                "averages": {}
            }, status=200)

        # Iterate through each student's evaluated answers
        for student in subject_data['students']:
            for answer in student.get('evaluated_answers', []):
                question_no = answer.get('question_no')
                marks = answer.get('marks_awarded', 0)

                # Validate question_no and marks
                if not isinstance(question_no, str) or not isinstance(marks, (int, float)):
                    logger.warning(f"Invalid answer data for student: {student.get('name', 'Unknown')}")
                    continue

                # Update sums and counts
                question_sums[question_no] = question_sums.get(question_no, 0) + marks
                question_counts[question_no] = question_counts.get(question_no, 0) + 1

        # Calculate averages
        question_averages = {
            qn: round(question_sums[qn] / question_counts[qn], 2)
            if question_counts[qn] > 0 else 0
            for qn in question_sums
        }

        # Sort averages by question number for consistent output
        sorted_averages = dict(sorted(question_averages.items()))

        # Prepare response
        response = {
            "subject_code": subject_code,
            "exam_id": exam_id,
            "student_count": student_count,
            "averages": sorted_averages
        }

        logger.info(f"Successfully calculated averages for subject_code: {subject_code}, exam_id: {exam_id}")
        return JsonResponse(response, status=200)

    except PyMongoError as e:
        logger.error(f"MongoDB error: {str(e)}")
        return JsonResponse({"error": "Database error occurred"}, status=500)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JsonResponse({"error": "An unexpected error occurred"}, status=500) 
    
@csrf_exempt
@require_POST
def upload_staff_marks(request):
    try:
        # Extract exam_id and subject_code from the request
        exam_id = request.POST.get('exam_id')
        subject_code = request.POST.get('subject_code')

        if not exam_id or not subject_code:
            return JsonResponse({'error': 'Missing exam_id or subject_code'}, status=400)

        # Check if the CSV file is present in the request
        if 'csv_file' not in request.FILES:
            return JsonResponse({'error': 'No CSV file provided'}, status=400)

        csv_file = request.FILES['csv_file']

        # Validation patterns
        reg_num_pattern = r'^(\d{4})(\d{2})([A-Za-z]{2})(\d{3})$'  # Register number format
        name_pattern = r'^[a-zA-Z\s\.\-]+$'  # Name format
        valid_depts = {"AD", "SB", "CD", "CE", "AM", "EE", "EC", "IT", "ME", "CS", "CT", "CV"}

        # Initialize errors list
        errors = []
        valid_rows = []
        student_collection = db["student"]

        # Read the CSV file
        csv_data = csv_file.read().decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_data))

        # Check if required columns are present
        required_columns = {'Name', 'Register number', 'Mark'}
        if not all(col in csv_reader.fieldnames for col in required_columns):
            missing_cols = required_columns - set(csv_reader.fieldnames)
            return JsonResponse({'error': f'Missing required columns in CSV: {", ".join(missing_cols)}'}, status=400)

        # Validate each row
        for row_num, row in enumerate(csv_reader, start=1):
            register_number = row.get('Register number', '').strip()
            staff_mark = row.get('Mark', '').strip()
            name = row.get('Name', '').strip()

            row_errors = []

            # Check for missing data
            if not register_number or not staff_mark or not name:
                row_errors.append(f"Row {row_num}: Missing Name, Register number, or Mark")
                errors.append(row_errors)
                continue

            # Validate register_number format
            match = re.match(reg_num_pattern, register_number)
            if not match:
                row_errors.append(f"Row {row_num}: Invalid Register number format: {register_number}")
            else:
                college_code, year_batch, dept, num = match.groups()
                if dept.upper() not in valid_depts:
                    row_errors.append(f"Row {row_num}: Invalid department in Register number: {dept}")
                if not num.isdigit():
                    row_errors.append(f"Row {row_num}: Non-numeric sequence in Register number: {num}")

            # Validate name format
            if not re.match(name_pattern, name):
                row_errors.append(f"Row {row_num}: Invalid characters in Name: {name}")

            # Validate staff_mark
            try:
                mark = int(staff_mark)
                if mark < 0:
                    row_errors.append(f"Row {row_num}: Mark must be a positive number: {staff_mark}")
            except ValueError:
                row_errors.append(f"Row {row_num}: Invalid Mark format: {staff_mark}")

            # Check if register_number exists in student collection
            if not student_collection.find_one({"register_number": register_number}):
                row_errors.append(f"Row {row_num}: Register number not found in database: {register_number}")

            # If no errors, store valid row data
            if not row_errors:
                valid_rows.append({
                    "register_number": register_number,
                    "staff_mark": mark,
                    "name": name
                })
            else:
                errors.append(row_errors)

        # If there are errors, return them
        if errors:
            return JsonResponse({'errors': errors}, status=400)

        # Update the database for valid rows
        for row in valid_rows:
            register_number = row['register_number']
            staff_mark = row['staff_mark']

            results_collection.update_one(
                {
                    "exam_id": exam_id,
                    "results.subjects.subject_code": subject_code,
                    "results.subjects.students.register_number": register_number
                },
                {
                    "$set": {
                        "results.subjects.$[subject].students.$[student].staff_mark": staff_mark
                    }
                },
                array_filters=[
                    {"subject.subject_code": subject_code},
                    {"student.register_number": register_number}
                ]
            )

        return JsonResponse({'message': 'Staff marks updated successfully'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
@csrf_exempt
def revert_answer_sheet(request):
    """
    Deletes an uploaded answer sheet and its associated marks from the database and AWS S3 storage.
    Accepts JSON payload: { "exam_id": "...", "register_number": "...", "subject_code": "..." }
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Accept JSON payload
        try:
            data = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        exam_id = data.get("exam_id")
        subject_code = data.get("subject_code")
        register_number = data.get("register_number")

        if not all([exam_id, subject_code, register_number]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Find the answer sheet in the database (nested structure)
        answer_sheet_doc = answer_sheet_collection.find_one({
            "exam_id": exam_id,
            "subjects": {
                "$elemMatch": {
                    "subject_code": subject_code,
                    "students": {
                        "$elemMatch": {
                            "student_id": register_number
                        }
                    }
                }
            }
        })

        if not answer_sheet_doc:
            return JsonResponse({"error": "Answer sheet not found"}, status=404)

        # Find and remove the student from the subject's students list
        file_url = None
        subject_idx = None
        student_idx = None
        for i, subject in enumerate(answer_sheet_doc.get("subjects", [])):
            if subject.get("subject_code") == subject_code:
                for j, student in enumerate(subject.get("students", [])):
                    if student.get("student_id") == register_number:
                        file_url = student.get("file_url")
                        subject_idx = i
                        student_idx = j
                        break
                if file_url:
                    break

        if not file_url:
            return JsonResponse({"error": "No file URL found for this answer sheet"}, status=404)

        # Delete the file from AWS S3
        try:
            s3_key = file_url.replace(f"{AWS_S3_CUSTOM_DOMAIN}/", "")
            s3_client.delete_object(Bucket=AWS_STORAGE_BUCKET_NAME, Key=s3_key)
        except Exception as s3_error:
            return JsonResponse({"error": f"Failed to delete PDF from S3: {str(s3_error)}"}, status=500)

        # Remove the student from the subject's students array
        update_result = answer_sheet_collection.update_one(
            {"_id": answer_sheet_doc["_id"], f"subjects.subject_code": subject_code},
            {"$pull": {f"subjects.$.students": {"student_id": register_number}}}
        )

        # Optionally, remove the subject if no students remain
        answer_sheet_doc = answer_sheet_collection.find_one({"_id": answer_sheet_doc["_id"]})
        for subject in answer_sheet_doc.get("subjects", []):
            if subject.get("subject_code") == subject_code and not subject.get("students"):
                answer_sheet_collection.update_one(
                    {"_id": answer_sheet_doc["_id"]},
                    {"$pull": {"subjects": {"subject_code": subject_code}}}
                )

        # Optionally, delete the whole document if no subjects remain
        answer_sheet_doc = answer_sheet_collection.find_one({"_id": answer_sheet_doc["_id"]})
        if answer_sheet_doc and not answer_sheet_doc.get("subjects"):
            answer_sheet_collection.delete_one({"_id": answer_sheet_doc["_id"]})

        # Delete the associated marks from the results collection
        results_collection.update_many(
            {"exam_id": exam_id},
            {"$pull": {
                "results.subjects.$[subject].students": {
                    "register_number": register_number
                }
            }},
            array_filters=[{"subject.subject_code": subject_code}]
        )

        return JsonResponse({"message": "Answer sheet and associated marks deleted successfully"}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500) 
import pymongo 
@csrf_exempt
def get_iae_details_by_ids(request):
    """
    Fetches student details, IAE scores (IAE-1, IAE-2, IAE-3), and answer_sheet_id for a given register number and subject code from the database.
    Expects a POST request with JSON containing 'register_number' and 'subject_code'.
    Returns a JSON response with student info, IAE scores, and answer_sheet_id for each exam, or an error if data is missing or not found.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
        register_number = body.get('register_number')
        subject_code = body.get('subject_code')

        if not register_number or not subject_code:
            return JsonResponse({'error': 'Missing register_number or subject_code'}, status=400)

        # Fetch student personal details
        student = student_collection.find_one({'register_number': register_number}, {'_id': 0})
        if not student:
            return JsonResponse({'error': 'No student found with register number'}, status=404)

        student_info = {
            'name': student.get('name'),
            'register_number': student.get('register_number'),
            'department': student.get('department'),
            'section': student.get('section'),
            'email': student.get('email'),
            'subject_code': subject_code
        }

        iae_scores = {
            'IAE - 1': {'total_marks': None, 'answer_sheet_id': None},
            'IAE - 2': {'total_marks': None, 'answer_sheet_id': None},
            'IAE - 3': {'total_marks': None, 'answer_sheet_id': None}
        }
        subject_name = None

        # Fetch all exam documents (IAE-1, IAE-2, IAE-3)
        exam_documents = results_collection.find({
            'exam_type': {'$in': ['IAE - 1', 'IAE - 2', 'IAE - 3']},
            'results.subjects.students.register_number': register_number,
            'results.subjects.subject_code': subject_code
        })

        for exam_doc in exam_documents:
            exam_type = exam_doc.get('exam_type')
            subjects = exam_doc.get('results', {}).get('subjects', [])

            for subject in subjects:
                if subject.get('subject_code') == subject_code:
                    for student_entry in subject.get('students', []):
                        if student_entry.get('register_number') == register_number:
                            total_marks = student_entry.get('total_marks')
                            answer_sheet_id = student_entry.get('answer_sheet_id')  # Fetch answer_sheet_id
                            iae_scores[exam_type] = {
                                'total_marks': total_marks,
                                'answer_sheet_id': answer_sheet_id
                            }
                            if not subject_name:
                                subject_name = subject.get('subject_name')
                            break

        # Add subject_name to student_info if found
        if subject_name:
            student_info['subject_name'] = subject_name

        return JsonResponse({
            **student_info,
            'IAE_scores': iae_scores,
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    
    

#======================================================= Subject Reports Stats =======================================================#

from django.http import JsonResponse
from collections import defaultdict, Counter
from pymongo import MongoClient

 # Replace with your actual database name
def extract_numeric_value(mark_str):
    # Use regular expression to find numeric values in the string
    match = re.search(r"[-+]?\d*\.\d+|\d+", mark_str)
    if match:
        return float(match.group())
    return 0.0

@csrf_exempt
def calculate_exam_statistics(request, exam_id=None, subject_code=None):
    """
    Function to collect data from MongoDB and calculate key statistics.
    Can be called with either:
    - URL parameters (exam_id, subject_code)
    - Query parameters (department, year, subject_code)
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # First try to use URL parameters if provided
        if exam_id and subject_code:
            # Find exam details to get department and year
            exam_details = exam_collection.find_one({"_id": ObjectId(exam_id)})
            if not exam_details:
                return JsonResponse({"error": "Exam not found"}, status=404)
            
            department = exam_details.get("department")
            year = exam_details.get("year")
        else:
            # Otherwise use query parameters
            department = request.GET.get("department")
            year = request.GET.get("year")
            subject_code = request.GET.get("subject_code")

        if not department or not year or not subject_code:
            return JsonResponse({"error": "Department, year, and subject_code are required"}, status=400)

        # Initialize statistics
        stats = {
            "IAE_vs_Semester": {},
            "Grade_Distribution": {
                "O+": 0, "A+": 0, "A": 0, "B+": 0, "B": 0, "F": 0
            },
            "CO_Mapping_Report": {},
            "Pass_Percentage": {
                "total": 0,
                "passed": 0,
                "percentage": 0.0
            },
            "Bloom_Taxonomy_Performance": {},
            "AI_vs_Manual_Discrepancy": {
                "avg_ai_marks": 0,
                "avg_staff_marks": 0
            }
        }

        # Fetch students from the specified department and year
        students_cursor = student_collection.find({"department": department, "year": year})
        
        # Track overall statistics
        total_students = 0
        total_ai_marks = 0
        total_staff_marks = 0
        ia_marks = defaultdict(int)
        ia_count = defaultdict(int)
        bloom_marks = defaultdict(list)
        co_mapping = defaultdict(lambda: {"total": 0, "marks": 0})
        discrepancies = []

        for student in students_cursor:
            register_number = student.get("register_number")
            
            # Find results for this student for the specific subject
            result_docs = results_collection.find({
                "results.subjects.subject_code": subject_code,
                "results.subjects.students.register_number": register_number
            })
            
            # Initialize student-specific variables
            student_ai_mark = 0
            student_staff_mark = 0
            student_total_marks = 0
            student_has_marks = False
            
            # Process all exam types for this student
            for result in result_docs:
                exam_type = result.get("exam_type", "Unknown")
                
                for subject in result.get("results", {}).get("subjects", []):
                    if subject.get("subject_code") == subject_code:
                        for s in subject.get("students", []):
                            if s.get("register_number") == register_number:
                                total_marks = s.get("total_marks", 0)
                                if not isinstance(total_marks, (int, float)):
                                    total_marks = 0
                                
                                staff_mark = s.get("staff_mark", 0)
                                if not isinstance(staff_mark, (int, float)):
                                    staff_mark = 0
                                
                                # Track if student has any marks
                                if total_marks > 0 or staff_mark > 0:
                                    student_has_marks = True
                                
                                # Accumulate marks for different exam types
                                if exam_type == "Semester":
                                    student_total_marks = total_marks
                                    student_staff_mark = staff_mark
                                    student_ai_mark = total_marks
                                
                                # Track IAE marks for comparison
                                if exam_type.startswith("IAE"):
                                    ia_key = exam_type
                                    if isinstance(total_marks, (int, float)) and total_marks > 0:
                                        ia_marks[ia_key] += total_marks * 2  # Double IAE marks
                                        ia_count[ia_key] += 1
                                
                                # Process evaluated answers for detailed metrics
                                for answer in s.get("evaluated_answers", []):
                                    # Extract bloom level and course outcome info
                                    bloom_level = answer.get("bloom_level", "Unknown")
                                    co = answer.get("co", "Unknown")
                                    marks_awarded = answer.get("marks_awarded", 0)
                                    
                                    # Maximum possible marks from rubric items
                                    max_marks = sum(item.get("marks", 0) for item in answer.get("rubric_items", []))
                                    
                                    # Calculate manual marks from rubric_marks
                                    manual_marks = 0
                                    for mark_item in answer.get("rubric_marks", []):
                                        mark_value = mark_item.get("mark", "0")
                                        if isinstance(mark_value, (int, float)):
                                            manual_marks += mark_value
                                        elif isinstance(mark_value, str):
                                            try:
                                                manual_marks += float(extract_numeric_value(mark_value))
                                            except:
                                                pass
                                    
                                    # Track Bloom's taxonomy performance
                                    if marks_awarded > 0:
                                        bloom_marks[bloom_level].append(marks_awarded)
                                    
                                    # Track course outcome achievement
                                    if co != "Unknown" and max_marks > 0:
                                        co_mapping[co]["total"] += max_marks
                                        co_mapping[co]["marks"] += marks_awarded
                                    
                                    # Record AI vs manual discrepancies
                                    if exam_type == "Semester" and answer.get("method_used") in ["gemini", "keyword"]:
                                        discrepancies.append({
                                            "student": s.get("name", "Unknown"),
                                            "question_no": answer.get("question_no", "Unknown"),
                                            "ai_marks": marks_awarded,
                                            "manual_marks": manual_marks,
                                            "difference": marks_awarded - manual_marks
                                        })
            
            # Only count students who have attempted exams
            if student_has_marks:
                total_students += 1
                total_ai_marks += student_ai_mark
                total_staff_marks += student_staff_mark
                
                # Update pass percentage stats
                stats["Pass_Percentage"]["total"] += 1
                if student_total_marks >= 50:
                    stats["Pass_Percentage"]["passed"] += 1
                
                # Update grade distribution
                if student_total_marks >= 90:
                    grade = "O+"
                elif student_total_marks >= 75:
                    grade = "A+"
                elif student_total_marks >= 60:
                    grade = "A"
                elif student_total_marks >= 50:
                    grade = "B+"
                elif student_total_marks >= 40:
                    grade = "B"
                else:
                    grade = "F"
                
                stats["Grade_Distribution"][grade] += 1
        
        # Calculate pass percentage
        if stats["Pass_Percentage"]["total"] > 0:
            stats["Pass_Percentage"]["percentage"] = (stats["Pass_Percentage"]["passed"] / stats["Pass_Percentage"]["total"]) * 100
        
        # Calculate average bloom's taxonomy performance
        for bloom, marks_list in bloom_marks.items():
            if marks_list:
                stats["Bloom_Taxonomy_Performance"][bloom] = round(sum(marks_list) / len(marks_list), 2)
            else:
                stats["Bloom_Taxonomy_Performance"][bloom] = 0
        
        # Calculate IAE vs Semester averages
        for ia_key, total in ia_marks.items():
            if ia_count[ia_key] > 0:
                stats["IAE_vs_Semester"][ia_key] = round(total / ia_count[ia_key], 2)
        
        # Add semester average if available
        if total_students > 0:
            stats["IAE_vs_Semester"]["Semester"] = round(total_ai_marks / total_students, 2)
        
        # Calculate average AI and staff marks
        if total_students > 0:
            stats["AI_vs_Manual_Discrepancy"] = {
                "avg_ai_marks": round(total_ai_marks / total_students, 2),
                "avg_staff_marks": round(total_staff_marks / total_students, 2),
                "discrepancy_details": discrepancies[:10]  # Include top 10 details for reference
            }
        
        # Calculate CO achievement percentages
        for co, data in co_mapping.items():
            if data["total"] > 0:
                achievement_percent = (data["marks"] / data["total"]) * 100
                stats["CO_Mapping_Report"][co] = {
                    "total_marks": data["total"],
                    "achieved_marks": data["marks"],
                    "achievement_percentage": round(achievement_percent, 2)
                }
        
        # Convert Grade Distribution to percentages
        if total_students > 0:
            for grade, count in stats["Grade_Distribution"].items():
                stats["Grade_Distribution"][grade] = round((count / total_students) * 100, 2)

        return JsonResponse(stats, status=200)

    except Exception as e:
        return JsonResponse({
            "error": str(e),
            "statistics": {
                "IAE_vs_Semester": {},
                "Grade_Distribution": {},
                "CO_Mapping_Report": {},
                "Pass_Percentage": {"total": 0, "passed": 0, "percentage": 0.0},
                "Bloom_Taxonomy_Performance": {},
                "AI_vs_Manual_Discrepancy": {"avg_ai_marks": 0, "avg_staff_marks": 0}
            }
        }, status=500)
        
#======================================================= get Overall exam details  =======================================================#
def get_sem_exam_details(request, exam_id, subject_code):
    """
    Fetch detailed exam data for a specific exam_id and subject_code from the exam_details collection.
    """

    try:
        # Query MongoDB for the document with matching _id and subject_code
        document = exam_collection.find_one({
            "_id": ObjectId(exam_id),
            "subjects.subject_code": subject_code
        })
    except Exception as e:
        return JsonResponse({"error": f"Invalid exam_id or query error: {str(e)}"}, status=400)

    if not document:
        return JsonResponse({"error": "No matching exam found."}, status=404)

    # Extract the matching subject
    subject_data = next(
        (subject for subject in document.get("subjects", []) if subject.get("subject_code") == subject_code),
        None
    )

    if not subject_data:
        return JsonResponse({"error": "Subject code not found in this exam."}, status=404)

    # Prepare the response JSON
    response_data = {
        "exam_type": document.get("exam_type"),
        "department": document.get("department"),
        "year": document.get("year"),
        "semester": document.get("semester"),
        "batch": document.get("batch"),
        "section": document.get("section"),
        
    }

    return JsonResponse(response_data, status=200)


#======================================================= get IA Exam Details  =======================================================#

@csrf_exempt
def get_sem_exam_performance(request, exam_id, subject_code, exam_type):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method. Only GET is allowed."}, status=405)

    print(f"Debug: Received request for exam_id={exam_id}, subject_code={subject_code}, exam_type={exam_type}")

    if not subject_code or not exam_type:
        return JsonResponse({"error": "Missing required parameters subject_code or exam_type"}, status=400)

    try:
        # Get exam details to find department and year
        exam_details = exam_collection.find_one({"_id": ObjectId(exam_id)})
        if not exam_details:
            return JsonResponse({"error": f"Exam not found with ID: {exam_id}"}, status=404)
        
        department = exam_details.get("department")
        year = exam_details.get("year")
        
        if not department or not year:
            return JsonResponse({"error": "Missing department or year information in exam record"}, status=404)
        
        print(f"Using department={department}, year={year} for aggregating data")

        # Normalize the exam type format
        normalized_exam_types = [
            exam_type,
            exam_type.replace(" - ", "-"),
            exam_type.replace("-", " - "),
            f"IAE - {exam_type[-1]}" if exam_type.startswith("IAE") else exam_type
        ]
        
        # Find all students with results for this department, year, subject code and any matching exam type
        students_with_results = []
        
        # Fetch students from the specified department and year
        students_cursor = student_collection.find({"department": department, "year": year})
        
        for student in students_cursor:
            register_number = student.get("register_number")
            
            # Find results for this student for the specific subject and exam type
            for et in normalized_exam_types:
                results = results_collection.find({
                    "exam_type": et,
                    "results.subjects.subject_code": subject_code,
                    "results.subjects.students.register_number": register_number
                })
                
                for result in results:
                    for subject in result.get("results", {}).get("subjects", []):
                        if subject.get("subject_code") == subject_code:
                            for student_result in subject.get("students", []):
                                if student_result.get("register_number") == register_number:
                                    students_with_results.append({
                                        "total_marks": student_result.get("total_marks", 0),
                                        "staff_mark": student_result.get("staff_mark", 0),
                                        "evaluated_answers": student_result.get("evaluated_answers", [])
                                    })
                                    # Once we find a matching result, no need to check other exam types
                                    break
                                    
                # If we found results for this student, no need to check other exam types
                if any(student.get("register_number") == register_number for student in students_with_results):
                    break

        if not students_with_results:
            return JsonResponse({
                "error": f"No student results found for {subject_code} with exam type {exam_type}",
                "department": department,
                "year": year
            }, status=404)

        # Calculate statistics
        marks = [student.get("total_marks", 0) for student in students_with_results]
        pass_marks = 50
        passed = [m for m in marks if m >= pass_marks]

        ai_scores = [student.get("total_marks", 0) for student in students_with_results]
        manual_scores = [student.get("staff_mark", 0) for student in students_with_results]

        ai_average = round(sum(ai_scores) / len(ai_scores), 2) if ai_scores else 0
        manual_average = round(sum(manual_scores) / len(manual_scores), 2) if manual_scores else 0

        # Process Bloom's taxonomy data
        bloom_scores = defaultdict(list)
        for student in students_with_results:
            for answer in student.get("evaluated_answers", []):
                level = answer.get("bloom_level", "Unknown")
                marks_awarded = answer.get("marks_awarded", 0)
                bloom_scores[level].append(marks_awarded)

        blooms_taxonomy_performance = {
            level: round(sum(scores) / len(scores), 2)
            for level, scores in bloom_scores.items() if scores
        }

        # Process question-wise averages
        question_totals = defaultdict(float)
        question_counts = defaultdict(int)
        
        for student in students_with_results:
            for answer in student.get("evaluated_answers", []):
                q_no = answer.get("question_no")
                marks_awarded = answer.get("marks_awarded", 0)
                if q_no is not None:
                    question_totals[q_no] += marks_awarded
                    question_counts[q_no] += 1

        question_wise_avg = {
            str(q): round(question_totals[q] / question_counts[q], 2)
            for q in question_totals if question_counts[q] > 0
        }

        data = {
            "class_average": round(sum(marks) / len(marks), 2) if marks else 0,
            "pass_count": len(passed),
            "fail_count": len(marks) - len(passed),
            "highest_mark": max(marks) if marks else 0,
            "ai_average_score": ai_average,
            "manual_average_score": manual_average,
            "blooms_taxonomy_performance": blooms_taxonomy_performance,
            "question_wise_average_score": question_wise_avg
        }

        return JsonResponse(data, status=200)

    except Exception as e:
        import traceback
        print(f"Error in get_sem_exam_performance: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({
            "error": str(e),
            "exam_id": exam_id,
            "subject_code": subject_code,
            "exam_type": exam_type
        }, status=500)


#======================================================= get Semester Exam Details Stats  =======================================================#

@csrf_exempt
def exam_analysis(request, exam_id, subject_code):
    if request.method != 'GET':
        return JsonResponse({"error": "Only GET method allowed"}, status=405)

    try:
        if not exam_id or not subject_code:
            return JsonResponse({"error": "exam_id and subject_code are required"}, status=400)

        db = client['COE']
        results_collection = db['results']

        # Aggregation pipeline
        pipeline = [
            {"$match": {
                "exam_id": exam_id,
                "results.subjects.subject_code": subject_code
            }},
            {"$unwind": "$results.subjects"},
            {"$match": {"results.subjects.subject_code": subject_code}},
            {"$unwind": "$results.subjects.students"},
            {"$project": {
                "total_marks": "$results.subjects.students.total_marks",
                "staff_mark": "$results.subjects.students.staff_mark",
                "evaluated_answers": "$results.subjects.students.evaluated_answers",
                "register_number": "$results.subjects.students.register_number"
            }},
            {"$unwind": "$evaluated_answers"},
            {"$group": {
                "_id": {
                    "question_no": "$evaluated_answers.question_no",
                    "bloom_level": "$evaluated_answers.bloom_level",
                    "co": "$evaluated_answers.co"
                },
                "total_students": {"$addToSet": "$register_number"},
                "total_marks_sum": {"$sum": "$total_marks"},
                "highest_mark": {"$max": "$total_marks"},
                "staff_mark_sum": {"$sum": "$staff_mark"},
                "marks": {"$push": "$total_marks"},
                "question_marks": {"$sum": "$evaluated_answers.marks_awarded"},
                "question_attempts": {
                    "$sum": {"$cond": [{"$ne": ["$evaluated_answers.method_used", "skipped"]}, 1, 0]}
                },
                "question_possible_marks": {
                    "$sum": {"$sum": "$evaluated_answers.rubric_items.marks"}
                }
            }},
            {"$group": {
                "_id": None,
                "total_students": {"$first": {"$size": "$total_students"}},
                "total_marks_sum": {"$first": "$total_marks_sum"},
                "highest_mark": {"$first": "$highest_mark"},
                "staff_mark_sum": {"$first": "$staff_mark_sum"},
                "marks": {"$first": "$marks"},
                "question_data": {
                    "$push": {
                        "question_no": "$_id.question_no",
                        "bloom_level": "$_id.bloom_level",
                        "co": "$_id.co",
                        "marks": "$question_marks",
                        "attempts": "$question_attempts",
                        "possible_marks": "$question_possible_marks"
                    }
                }
            }},
            {"$project": {
                "class_average": {"$divide": ["$total_marks_sum", "$total_students"]},
                "highest_mark": 1,
                "total_students": 1,
                "staff_mark_sum": 1,
                "marks": 1,
                "question_data": 1
            }}
        ]

        result = list(results_collection.aggregate(pipeline))
        if not result:
            return JsonResponse({"error": "No data found for given exam_id and subject_code"}, status=404)

        result = result[0]
        total_students = result['total_students']
        total_marks = 100  # Total marks fixed for Semester exams

        # Grade Distribution and Pass/Fail Calculation
        grades = {"O": 0, "A+": 0, "A": 0, "B+": 0, "F": 0}
        pass_count = 0
        for mark in result['marks']:
            if mark >= 90:
                grades["O"] += 1
                pass_count += 1
            elif mark >= 80:
                grades["A+"] += 1
                pass_count += 1
            elif mark >= 70:
                grades["A"] += 1
                pass_count += 1
            elif mark >= 60:
                grades["B+"] += 1
                pass_count += 1
            else:
                grades["F"] += 1

        pass_percentage = (pass_count / total_students) * 100 if total_students > 0 else 0
        fail_percentage = 100 - pass_percentage

        # Bloom's and CO analysis
        blooms = {"remember": 0, "understand": 0, "analyze": 0}
        bloom_sums, bloom_possible, bloom_attempts = blooms.copy(), blooms.copy(), blooms.copy()

        co_sums = {f"CO{i}": 0 for i in range(1, 6)}
        co_possible = co_sums.copy()
        co_attempts = co_sums.copy()

        question_wise_averages = {}
        ai_score_sum = 0

        for q in result['question_data']:
            bloom_level = (q['bloom_level'] or '').lower()
            if bloom_level in ['rem', 'und', 'ana']:
                bloom_key = {'rem': 'remember', 'und': 'understand', 'ana': 'analyze'}[bloom_level]
                bloom_sums[bloom_key] += q['marks']
                bloom_possible[bloom_key] += q['possible_marks']
                bloom_attempts[bloom_key] += q['attempts']

            co = q['co'] or ''
            if co in co_sums:
                co_sums[co] += q['marks']
                co_possible[co] += q['possible_marks']
                co_attempts[co] += q['attempts']

            question_wise_averages[q['question_no']] = round(q['marks'] / q['attempts'], 2) if q['attempts'] > 0 else 0
            ai_score_sum += q['marks']

        for level in blooms:
            if bloom_attempts[level] > 0 and bloom_possible[level] > 0:
                blooms[level] = round((bloom_sums[level] / bloom_possible[level]) * 100, 2)
            else:
                blooms[level] = 0

        co_averages = {}
        co_percentages = {}
        for co in co_sums:
            if co_attempts[co] > 0:
                co_averages[co] = round(co_sums[co] / co_attempts[co], 2)
                co_percentages[co] = round((co_sums[co] / co_possible[co]) * 100, 2) if co_possible[co] > 0 else 0
            else:
                co_averages[co] = co_percentages[co] = 0

        ai_score_percentage = (ai_score_sum / (total_students * total_marks)) * 100 if total_students > 0 else 0
        staff_mark_percentage = (result['staff_mark_sum'] / (total_students * total_marks)) * 100 if total_students > 0 else 0

        response = {
            "class_average": round(result['class_average'], 2),
            "highest_mark": result['highest_mark'],
            "grades": grades,
            "pass_percentage": round(pass_percentage, 2),
            "fail_percentage": round(fail_percentage, 2),
            "blooms_percentages": blooms,
            "co_averages": co_averages,
            "co_percentages": co_percentages,
            "ai_score_percentage": round(ai_score_percentage, 2),
            "staff_mark_percentage": round(staff_mark_percentage, 2),
            "question_wise_averages": question_wise_averages
        }

        return JsonResponse(response)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
    
#======================================================= Student Analysis Function  =======================================================#

@csrf_exempt
def student_analysis(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        # Parse payload
        data = json.loads(request.body)
        register_number = data.get('register_number')
        subject_code = data.get('subject_code')

        if not register_number or not subject_code:
            return JsonResponse({'error': 'register_number and subject_code are required'}, status=400)

        # MongoDB connection
        db = client['COE']
        results_collection = db['results']
        student_collection = db['student']

        # Fetch personal info from student collection
        student_info = student_collection.find_one(
            {
                'register_number': register_number,
                '$or': [
                    {'status': 'Active'},
                    {'status': {'$exists': False}}
                ]
            },
            {'_id': 0}
        )
        if not student_info:
            return JsonResponse({'error': 'Student not found or not active'}, status=404)

        # Aggregation pipeline for results
        pipeline = [
            # Match subject_code and register_number
            {'$match': {
                'results.subjects.subject_code': subject_code,
            }},
            # Unwind subjects and students
            {'$unwind': '$results.subjects'},
            {'$match': {'results.subjects.subject_code': subject_code}},
            {'$unwind': '$results.subjects.students'},
            {'$match': {'results.subjects.students.register_number': register_number}},
            # Project relevant fields
            {'$project': {
                'exam_type': '$exam_type',
                'register_number': '$results.subjects.students.register_number',
                'total_marks': '$results.subjects.students.total_marks',
                'evaluated_answers': '$results.subjects.students.evaluated_answers',
                'created_at': '$results.subjects.students.created_at',
                'subject_name': '$results.subjects.subject_name'
            }},
            # Sort by created_at to get the most recent exam per type
            {'$sort': {'created_at': -1}},
            # Group by exam_type to collect marks and answers
            {'$group': {
                '_id': '$exam_type',
                'total_marks': {'$first': '$total_marks'},
                'evaluated_answers': {'$first': '$evaluated_answers'},
                'subject_name': {'$first': '$subject_name'}
            }}
        ]

        # Execute pipeline to get marks for each exam type
        results = list(results_collection.aggregate(pipeline))

        # Initialize IAE and Semester marks (default to 0 if not found)
        iae_marks = {
            'IAE-1': 0,
            'IAE-2': 0,
            'IAE-3': 0,
            'Semester': 0
        }

        # Initialize AI vs Staff marks comparison
        ai_vs_staff_marks = {
            'question_wise_ai_marks': {},
            'question_wise_staff_marks': {}
        }

        # Process results to populate IAE marks and AI vs Staff marks
        latest_semester_result = None
        for result in results:
            exam_type = result['_id']
            total_marks = result['total_marks']
            evaluated_answers = result['evaluated_answers']
            subject_name = result['subject_name']

            # Populate IAE and Semester marks
            if exam_type in iae_marks:
                iae_marks[exam_type] = total_marks

            # Store the latest Semester result for further processing
            if exam_type == 'Semester':
                latest_semester_result = result

            # Collect AI vs Staff marks (from the latest exam per type, but we'll prioritize Semester below)
            for answer in evaluated_answers:
                question_no = answer['question_no']
                staff_marks = answer.get('marks_awarded', 0)
                ai_marks = answer.get('ai_marks', 0)  # Assuming ai_marks field exists; default to 0 if not

                # Initialize if not already present
                if question_no not in ai_vs_staff_marks['question_wise_ai_marks']:
                    ai_vs_staff_marks['question_wise_ai_marks'][question_no] = 0
                    ai_vs_staff_marks['question_wise_staff_marks'][question_no] = 0

                # Update AI and Staff marks (we'll overwrite with Semester data if available)
                ai_vs_staff_marks['question_wise_ai_marks'][question_no] = ai_marks
                ai_vs_staff_marks['question_wise_staff_marks'][question_no] = staff_marks

        # If no results found for the student and subject, return defaults
        if not results:
            return JsonResponse({
                'subject_name': '',
                'personal_info': student_info,
                'blooms_percentages': {'remember': 0.0, 'understand': 0.0, 'analyze': 0.0},
                'question_wise_marks': {},
                'question_max_marks': {
                    '1': 2, '2': 2, '3': 2, '4': 2, '5': 2, '6': 13, '7': 13, '8': 14
                },
                'class_rank': 0,
                'grade': 'N/A',
                'iae_marks': iae_marks,
                'ai_vs_staff_marks': ai_vs_staff_marks
            })

        # Process the latest Semester result for student-specific data (question-wise marks, blooms, rank, etc.)
        if not latest_semester_result:
            # If no Semester result, use the latest result available
            latest_semester_result = results[0]
        
        student_data = latest_semester_result
        subject_name = student_data['subject_name']

        # Fetch all marks for class ranking (separate query for all students in the subject)
        rank_pipeline = [
            {'$match': {
                'results.subjects.subject_code': subject_code,
                'exam_type': 'Semester'
            }},
            {'$unwind': '$results.subjects'},
            {'$match': {'results.subjects.subject_code': subject_code}},
            {'$unwind': '$results.subjects.students'},
            {'$project': {
                'total_marks': '$results.subjects.students.total_marks'
            }}
        ]
        all_marks_result = list(results_collection.aggregate(rank_pipeline))
        all_marks = sorted([r['total_marks'] for r in all_marks_result], reverse=True)

        # Calculate grade
        total_marks = student_data['total_marks']
        if total_marks >= 90:
            grade = 'O'
        elif total_marks >= 80:
            grade = 'A+'
        elif total_marks >= 70:
            grade = 'A'
        elif total_marks >= 60:
            grade = 'B+'
        else:
            grade = 'F'

        # Calculate class rank
        rank = 1
        for mark in all_marks:
            if mark > total_marks:
                rank += 1
            elif mark == total_marks:
                continue
            else:
                break

        # Calculate Bloom's percentages
        blooms = {'remember': 0.0, 'understand': 0.0, 'analyze': 0.0}
        bloom_marks = {'remember': 0, 'understand': 0, 'analyze': 0}
        bloom_possible_marks = {'remember': 0, 'understand': 0, 'analyze': 0}

        # Calculate question-wise marks (from Semester exam)
        question_wise_marks = {}

        for answer in student_data['evaluated_answers']:
            question_no = answer['question_no']
            marks_awarded = answer.get('marks_awarded', 0)
            possible_marks = sum(item.get('marks', 0) for item in answer.get('rubric_items', []))
            bloom_level = answer.get('bloom_level', '').lower()

            # Store raw marks_awarded
            question_wise_marks[question_no] = marks_awarded

            # Bloom's calculations
            if bloom_level in ['rem', 'und', 'ana']:
                bloom_key = {'rem': 'remember', 'und': 'understand', 'ana': 'analyze'}[bloom_level]
                bloom_marks[bloom_key] += marks_awarded
                bloom_possible_marks[bloom_key] += possible_marks

        # Finalize Bloom's percentages
        for level in blooms:
            if bloom_possible_marks[level] > 0:
                blooms[level] = round((bloom_marks[level] / bloom_possible_marks[level]) * 100, 2)
            else:
                blooms[level] = 0.0

        # Define question maximum marks
        question_max_marks = {
            '1': 2, '2': 2, '3': 2, '4': 2, '5': 2, '6': 13, '7': 13, '8': 14
        }

        # JSON response
        response = {
            'subject_name': subject_name,
            'personal_info': student_info,
            'blooms_percentages': blooms,
            'question_wise_marks': question_wise_marks,
            'question_max_marks': question_max_marks,
            'class_rank': rank,
            'grade': grade,
            'iae_marks': iae_marks,  # Added IAE marks
            'ai_vs_staff_marks': ai_vs_staff_marks  # Added AI vs Staff marks
        }

        return JsonResponse(response)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)
    
    
#======================================================= get all Students Marks  =======================================================#

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import statistics
from bson import ObjectId

@csrf_exempt
def get_students_with_detailed_results(request):
    """
    Returns detailed student data for a specific department, year, and subject, including IAE-1, IAE-2, IAE-3 (doubled),
    Semester marks, Attendance, AI marks, Department, Section, and Pass/Fail/Absent status.
    Pass criteria: IAE marks >= 25 (after doubling), Semester marks >= 50.
    Query parameters: department (e.g., 'CSE'), year (e.g., '2022'), subject_code (e.g., 'CS101').
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Get query parameters
        department = request.GET.get("department")
        year = request.GET.get("year")
        subject_code = request.GET.get("subject_code")

        if not department or not year or not subject_code:
            return JsonResponse({"error": "Department, year, and subject_code are required"}, status=400)

        # Fetch students from the specified department and year
        students_cursor = student_collection.find({"department": department, "year": year})
        students_data = []

        for idx, student in enumerate(students_cursor, start=1):
            register_number = student.get("register_number")
            student_name = student.get("name", "-")
            student_id = str(student.get("_id"))
            student_department = student.get("department", "-")
            section = student.get("section", "-")  # Default to "-" if section is missing

            # Default values
            iae1, iae2, iae3, semester = "-", "-", "-", "-"
            attendance = "-"
            ai_mark = "-"
            status = "Absent"

            # Find results for this student for the specific subject
            result_docs = results_collection.find({
                "results.subjects.subject_code": subject_code,
                "results.subjects.students.register_number": register_number
            })

            # Initialize lists to store marks
            iae1_marks = []
            iae2_marks = []
            iae3_marks = []
            semester_marks = []
            staff_marks = []
            has_attempted_any_exam = False

            for result in result_docs:
                exam_type = result.get("exam_type")
                for subject in result.get("results", {}).get("subjects", []):
                    if subject.get("subject_code") == subject_code:
                        for s in subject.get("students", []):
                            if s.get("register_number") == register_number:
                                total_marks = s.get("total_marks", "-")
                                subject_attendance = s.get("attendance", "-")
                                subject_ai_mark = sum(
                                    answer.get("marks_awarded", 0)
                                    for answer in s.get("evaluated_answers", [])
                                    if isinstance(answer.get("marks_awarded"), (int, float))
                                ) if s.get("evaluated_answers") else "-"
                                staff_mark = s.get("staff_mark", "-")

                                # Mark as attempted if any valid marks exist
                                if isinstance(total_marks, (int, float)) or isinstance(staff_mark, (int, float)):
                                    has_attempted_any_exam = True

                                # Store marks based on exam type, doubling IAE marks
                                if exam_type == "IAE - 1" and isinstance(total_marks, (int, float)):
                                    iae1_marks.append(total_marks * 2)
                                elif exam_type == "IAE - 2" and isinstance(total_marks, (int, float)):
                                    iae2_marks.append(total_marks * 2)
                                elif exam_type == "IAE - 3" and isinstance(total_marks, (int, float)):
                                    iae3_marks.append(total_marks * 2)
                                elif exam_type == "Semester" and isinstance(total_marks, (int, float)):
                                    semester_marks.append(total_marks)

                                # Store staff marks for semester
                                if exam_type == "Semester" and isinstance(staff_mark, (int, float)):
                                    staff_marks.append(staff_mark)

                                # Update attendance and AI mark if available
                                if subject_attendance != "-":
                                    attendance = subject_attendance
                                if subject_ai_mark != "-":
                                    ai_mark = subject_ai_mark

            # Calculate average marks for each exam type
            iae1 = round(statistics.mean(iae1_marks), 2) if iae1_marks else "-"
            iae2 = round(statistics.mean(iae2_marks), 2) if iae2_marks else "-"
            iae3 = round(statistics.mean(iae3_marks), 2) if iae3_marks else "-"
            semester = round(statistics.mean(semester_marks), 2) if semester_marks else "-"
            final_mark = round(statistics.mean(staff_marks), 2) if staff_marks else semester

            # Determine pass/fail/absent status
            if has_attempted_any_exam:
                if isinstance(final_mark, (int, float)):
                    status = "Pass" if final_mark >= 50 else "Fail"
                elif any(isinstance(m, (int, float)) and m >= 25 for m in [iae1, iae2, iae3]):
                    status = "Fail"  # Attempted IAE but no semester marks
                else:
                    status = "Fail"  # Attempted but no sufficient marks
            else:
                status = "Absent"  # No exams attempted

            student_record = {
                "register_number": register_number,
                "name": student_name,
                "iae1": iae1,
                "iae2": iae2,
                "iae3": iae3,
                "semester": semester,
                "attendance": attendance,
                "exam_mark": final_mark if final_mark != "-" else iae3,
                "ai_mark": ai_mark,
                "status": status,
                "student_id": student_id,
                "department": student_department,
                "section": section
            }

            students_data.append(student_record)

        return JsonResponse({"students": students_data}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def get_student_exam_details(request, exam_id=None, subject_code=None, register_number=None):
    """
    Returns detailed performance data for a specific student across different exam types.
    Includes marks, written answers, evaluator feedback, and question details.
    
    URL Parameters:
        - exam_id: The ID of the exam
        - subject_code: The subject code
        - register_number: Student's registration number
        
    Query Parameters (optional):
        - exam_type: Filter by specific exam type (IAE-1, IAE-2, IAE-3, Semester)
    
    Returns:
        Student performance details including marks, answers, questions and feedback.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Get query parameters
        exam_type_filter = request.GET.get("exam_type")
        
        if not all([exam_id, subject_code, register_number]):
            return JsonResponse({"error": "Missing required parameters"}, status=400)

        # Get student info
        student_info = student_collection.find_one({"register_number": register_number})
        if not student_info:
            return JsonResponse({"error": f"Student not found: {register_number}"}, status=404)
        
        # Create student profile object
        student_profile = {
            "register_number": register_number,
            "name": student_info.get("name", ""),
            "email": student_info.get("email", ""),
            "department": student_info.get("department", ""),
            "year": student_info.get("year", ""),
            "section": student_info.get("section", ""),
        }
        
        # Get exam details to find subject_name
        exam_details = None
        try:
            exam_details = exam_collection.find_one({"_id": ObjectId(exam_id)})
        except Exception as e:
            print(f"Warning: Could not find exam details: {str(e)}")
            
        subject_name = ""
        if exam_details:
            for subject in exam_details.get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    subject_name = subject.get("subject_name", "")
                    break
        
        # Get question mapping (for question text, max marks, etc.)
        question_mappings = {}
        try:
            # Try subject-specific mapping first
            mapped_questions = exam_mapped_questions_collection.find_one({
                "exam_id": exam_id,
                "subject_code": subject_code
            })
            
            if not mapped_questions:
                # Try without subject code if first query fails
                mapped_questions = exam_mapped_questions_collection.find_one({
                    "$or": [
                        {"exam_id": exam_id},
                        {"exam_id": str(exam_id)}
                    ]
                })
                
            if mapped_questions and "questions" in mapped_questions:
                for q in mapped_questions["questions"]:
                    q_no = q.get("question_no")
                    if q_no:
                        question_mappings[str(q_no)] = {
                            "question_text": q.get("question_text", ""),
                            "expected_answer": q.get("answer_text", ""),
                            "max_marks": q.get("marks", 0),
                            "bloom_level": q.get("bloom_level", ""),
                            "co": q.get("CO", "")
                        }
        except Exception as e:
            print(f"Warning: Could not load question mappings: {str(e)}")
            
        # Get extracted answers from answer sheet collection with enhanced details
        extracted_answers_map = {}
        
        # Find the answer sheet with nested structure
        answer_sheet = answer_sheet_collection.find_one({
            "exam_id": exam_id,
            "subjects": {
                "$elemMatch": {
                    "subject_code": subject_code,
                    "students": {
                        "$elemMatch": {
                            "student_id": register_number
                        }
                    }
                }
            }
        })
        
        # Process extracted answers from the answer sheet
        if answer_sheet:
            for subject in answer_sheet.get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    for student in subject.get("students", []):
                        if student.get("student_id") == register_number:
                            for answer in student.get("extracted_answers", []):
                                q_no = answer.get("question_no")
                                if q_no:
                                    # Store complete extracted answer data (preserving all fields)
                                    extracted_answers_map[str(q_no)] = answer
        
        # Find all results for this student and subject with exam_type filtering
        normalized_exam_types = {
            "IAE-1": ["IAE - 1", "IAE-1", "iae1", "IAE1"],
            "IAE-2": ["IAE - 2", "IAE-2", "iae2", "IAE2"],
            "IAE-3": ["IAE - 3", "IAE-3", "iae3", "IAE3"],
            "Semester": ["Semester", "semester", "SEMESTER"]
        }
        
        # Build query for results collection
        query = {
            "results.subjects.subject_code": subject_code,
            "results.subjects.students.register_number": register_number
        }
        
        if exam_type_filter:
            for norm_type, variants in normalized_exam_types.items():
                if exam_type_filter in variants:
                    query["exam_type"] = {"$in": variants}
                    break
                    
        # Find all applicable results
        result_docs = results_collection.find(query)
        
        # Process results into the response format
        exam_results = []
        
        for result in result_docs:
            exam_type = result.get("exam_type", "Unknown")
            # Determine normalized type
            normalized_type = None
            for norm_type, variants in normalized_exam_types.items():
                if exam_type in variants:
                    normalized_type = norm_type
                    break
            if not normalized_type:
                normalized_type = exam_type
                
            # Extract subject data
            for subject in result.get("results", {}).get("subjects", []):
                if subject.get("subject_code") == subject_code:
                    # Extract student data
                    for student in subject.get("students", []):
                        if student.get("register_number") == register_number:
                            # Get subject name if not already set
                            if not subject_name and subject.get("subject_name"):
                                subject_name = subject.get("subject_name")
                                
                            # Process all evaluated answers
                            processed_answers = []
                            for answer in student.get("evaluated_answers", []):
                                q_no = answer.get("question_no")
                                
                                # Skip questions where the student didn't provide an answer
                                justification = answer.get("justification", "")
                                reduction_reasons = answer.get("reduction_reasons", "")
                                method_used = answer.get("method_used", "")
                                
                                # Skip if answer was not found or skipped
                                if (method_used == "skipped" or 
                                    justification == "Answer not found in extracted content" or 
                                    reduction_reasons == "Answer not found in extracted content"):
                                    continue
                                
                                # Get question mapping data
                                q_mapping = question_mappings.get(str(q_no), {})
                                
                                # Get written answer data from extracted_answers_map
                                extracted_answer_data = extracted_answers_map.get(str(q_no), {})
                                
                                # Create comprehensive answer object, prioritizing data from different sources
                                processed_answer = {
                                    "question_no": q_no,
                                    # Prioritize extracted answer data for question fields
                                    "question_text": (
                                        extracted_answer_data.get("question_text") or 
                                        q_mapping.get("question_text", "")
                                    ),
                                    "max_marks": (
                                        extracted_answer_data.get("max_marks") or 
                                        q_mapping.get("max_marks", 0)
                                    ),
                                    # Evaluation fields from result collection
                                    "marks_awarded": answer.get("marks_awarded", 0),
                                    "ai_mark": answer.get("ai_marks", answer.get("marks_awarded", 0)),
                                    # Student's written answer from answer sheet
                                    "written_answer": (
                                        extracted_answer_data.get("answer_text", "")
                                    ),
                                    # Model answer from question mappings
                                    "expected_answer": q_mapping.get("expected_answer", ""),
                                    "feedback": answer.get("feedback", []),
                                    "justification": justification,
                                    "reduction_reasons": reduction_reasons,
                                    "method_used": method_used,
                                    "rubric_items": answer.get("rubric_items", []),
                                    "rubric_marks": answer.get("rubric_marks", []),
                                    # Bloom and CO info - prioritize extracted answer data over other sources
                                    "bloom_level": (
                                        extracted_answer_data.get("bloom_level") or 
                                        answer.get("bloom_level") or 
                                        q_mapping.get("bloom_level", "")
                                    ),
                                    "co": (
                                        extracted_answer_data.get("co") or 
                                        answer.get("co") or 
                                        q_mapping.get("co", "")
                                    )
                                }
                                processed_answers.append(processed_answer)
                                
                            # Add to exam_results if we have processed answers
                            if processed_answers:
                                exam_results.append({
                                    "exam_type": normalized_type,
                                    "total_marks": student.get("total_marks", 0),
                                    "staff_mark": student.get("staff_mark", 0),
                                    "questions_answered": len(processed_answers),
                                    "answers": processed_answers
                                })
        
        # --- Stats Calculation ---
        total_achieved_marks = 0
        total_raw_max_marks = 0

        for result in exam_results:
            exam_type = result["exam_type"]
            raw_marks = result.get("total_marks", 0)

            if exam_type == "Semester":
                total_raw_max_marks += 100
            else:
                total_raw_max_marks += 50

            total_achieved_marks += raw_marks

        # Normalize to 50 scale
        normalized_total = (total_achieved_marks / total_raw_max_marks) * 50 if total_raw_max_marks else 0
        percentage = (normalized_total / 50) * 100 if normalized_total else 0
        status = "Pass" if percentage >= 50 else "Fail"

        normalized_total = round(normalized_total, 2)
        percentage = round(percentage, 2)

        # --- Score Obtained Per Question (AI Marks) ---
        ai_score_per_question = {}
        #max_marks_per_question = {}

        for result in exam_results:
            for answer in result.get("answers", []):
                q_no = str(answer.get("question_no"))
                ai_mark = answer.get("ai_mark", 0)
                max_marks = answer.get("max_marks", 0)
                if q_no:
                    ai_score_per_question[q_no] = ai_mark
                    #max_marks_per_question[q_no] = max_marks


        # --- Bloom Level Usage Calculation ---
        bloom_counter = {}

        for result in exam_results:
            for answer in result.get("answers", []):
                bloom = answer.get("bloom_level")
                if bloom:
                    bloom_counter[bloom] = bloom_counter.get(bloom, 0) + 1

        
        # Final response
        response = {
            "student": student_profile,
            "subject_code": subject_code,
            "subject_name": subject_name,
            "stats": {
                "total_marks": total_achieved_marks,
                "staff_mark": sum(result.get("staff_mark", 0) for result in exam_results),
                "max_total_marks": normalized_total,
                "percentage": percentage,
                "status": status,
                "bloom_usage": bloom_counter
            },
            "score_per_question": ai_score_per_question,
            #"max_marks_per_question": max_marks_per_question,
            "exam_results": exam_results
        }
        
        return JsonResponse(response, status=200)
    
    except Exception as e:
        import traceback
        print(f"Error in get_student_exam_details: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({"error": str(e)}, status=500)