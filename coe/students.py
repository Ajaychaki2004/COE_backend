from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from datetime import datetime, timedelta
from pymongo import MongoClient
from io import TextIOWrapper
from django.views.decorators.http import require_POST
from bson import ObjectId
from dotenv import load_dotenv
import os
import json
import re
import jwt
from queue import Queue
from pdf2image import convert_from_path, pdfinfo_from_path
from io import BytesIO
import csv
from bson.objectid import ObjectId


load_dotenv()

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

#======================================================= STUDENTS ===========================================================================

@csrf_exempt
def get_all_students(request):
    """
    Returns all student entries from the MongoDB 'student' collection,
    including the '_id' field as a string.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        student_collection = db["student"]
        students_cursor = student_collection.find({})

        # Convert ObjectId to string and build the response list
        students = []
        for student in students_cursor:
            student['_id'] = str(student['_id'])
            students.append(student)

        return JsonResponse({"students": students}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def add_student(request):
    """
    Adds a new student to the 'student' collection.
    Prevents duplicates based on register_number and email.
    Validates department in register_number matches the selected department.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)

        name = data.get("name")
        register_number = data.get("register_number")
        college_name = data.get("college_name")
        department = data.get("department")
        year = data.get("year")
        section = data.get("section")
        email = data.get("email")
        batch = data.get("batch", "")

        # Initialize errors list
        errors = []

        if not all([name, register_number, college_name, department, year, section, email, batch]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Validation patterns
        reg_num_pattern = r'^(\d{4})(\d{2})([A-Za-z]{2})(\d{3})$'  # Updated pattern for new register number format
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        name_pattern = r'^[a-zA-Z\s\.\-]+$'
        section_pattern = r'^[A-Z]$'
        roman_to_numeric = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}
        current_year = datetime.now().year
        valid_depts = {"AD", "SB", "CD", "CE", "AM", "EE", "EC", "IT", "ME", "CS", "CT", "CV"}
        batch_pattern = r'^\d{4}\s*-\s*\d{4}$'

        # Mapping of dropdown departments to department codes in register_number
        dept_mapping = {
            "CSE": "CS",
            "CSD": "CD",
            "CST": "CT",
            "IT": "IT",
            "IOT": "SB",
            "AI&DS": "AD",
            "AI&ML": "AM",
            "ECE": "EC",
            "EEE": "EE",
            "Mech": "ME",
            "Civil": "CV"
        }

        # Validate batch
        if not re.match(batch_pattern, batch):
            errors.append(f"Invalid batch format: {batch}")

        # Validate register_number format
        reg_num = register_number.strip()
        match = re.match(reg_num_pattern, reg_num)
        if not match:
            errors.append(f"Invalid register_number format: {reg_num}")
        else:
            college_code, year_batch, dept, num = match.groups()
            if dept.upper() not in valid_depts:
                errors.append(f"Invalid department in register_number: {dept}")
            if not num.isdigit():
                errors.append(f"Non-numeric sequence in register_number: {num}")

            # Validate that the selected department matches the department code in register_number
            selected_dept_code = dept_mapping.get(department)
            if not selected_dept_code:
                errors.append(f"Invalid department selected: {department}")
            elif selected_dept_code != dept.upper():
                errors.append(f"Selected department ({department}) does not match the department in register_number ({dept})")

        # Validate name
        if not re.match(name_pattern, name.strip()):
            errors.append(f"Invalid characters in name: {name}")

        # Validate email
        if not re.match(email_pattern, email.strip()):
            errors.append(f"Invalid email format: {email}")

        # Validate year field (allow Roman numerals or numeric)
        year_val = year.strip()
        if year_val in roman_to_numeric:
            numeric_year = roman_to_numeric[year_val]
        else:
            try:
                numeric_year = int(year_val)
                if not 1 <= numeric_year <= 4:
                    errors.append(f"Invalid study year: {year_val}")
            except ValueError:
                errors.append(f"Invalid study year (must be I, II, III, IV or 1, 2, 3, 4): {year_val}")

        # Validate section
        if not re.match(section_pattern, section.strip()):
            errors.append(f"Invalid section format: {section}")

        student_collection = db["student"]

        # Duplicate check based on register number
        if student_collection.find_one({"register_number": register_number}):
            return JsonResponse({"error": "Student with this register number already exists"}, status=409)

        # Duplicate check based on email
        if student_collection.find_one({"email": email}):
            return JsonResponse({"error": "Student with this email already exists"}, status=409)

        # Return errors if any
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        student_doc = {
            "name": name,
            "register_number": register_number,
            "college_name": college_name,
            "department": department,
            "year": year,
            "section": section,
            "email": email,
            "batch": batch,
        }

        result = student_collection.insert_one(student_doc)

        return JsonResponse({
            "message": "Student added successfully"
        }, status=201)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
@require_POST
def preview_upload_students(request):
    """
    Accepts a CSV file, parses it, and provides a preview of the data with validation for edge cases.
    Allows Roman numerals (I, II, III, IV) for the year field.
    Removes year and department outlier detection.
    Accepts department codes in register_number in any case, converts to uppercase for validation.
    """
    try:
        if 'file' not in request.FILES:
            return JsonResponse({"error": "No file provided"}, status=400)

        csv_file = request.FILES['file']
        if csv_file.size > 5 * 1024 * 1024:
            return JsonResponse({"error": "File size exceeds 5MB limit"}, status=400)

        try:
            decoded_file = TextIOWrapper(csv_file.file, encoding='utf-8')
            reader = csv.DictReader(decoded_file)
        except csv.Error:
            return JsonResponse({"error": "File could not be parsed as CSV"}, status=400)

        required_fields = ["register_number", "name", "college_name", "department", "year", "section", "email", "batch"]
        if not all(field in reader.fieldnames for field in required_fields):
            return JsonResponse({"error": f"CSV missing required headers: {', '.join(set(required_fields) - set(reader.fieldnames))}"}, status=400)

        valid_rows = []
        invalid_rows = []
        register_numbers = set()
        emails = set()


        valid_depts = {"AD", "IOT", "CD", "CE", "AM", "EE", "ECE", "IT","ME"}
        reg_num_pattern = r'^(\d{4})(\d{2})([A-Za-z]{2})(\d{3})$'  # Updated pattern for new register number format
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        name_pattern = r'^[a-zA-Z\s\.\-]+$'
        section_pattern = r'^[A-Z]$'
        roman_to_numeric = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}
        current_year = datetime.now().year
        for row in reader:
            errors = []
            is_valid = True

            # Check for empty or whitespace-only fields
            for field in required_fields:
                if not row.get(field) or row[field].strip() == "":
                    errors.append(f"Missing or empty {field}")
                    is_valid = False

            if not is_valid:
                invalid_rows.append({"row": row, "errors": errors})
                continue

            # Validate register_number format
            reg_num = row["register_number"].strip()
            match = re.match(reg_num_pattern, reg_num)
            if not match:
                errors.append(f"Invalid register_number format: {reg_num}")
                is_valid = False
            else:
                college_code, year_batch, dept, num = match.groups()
                if dept.upper() not in valid_depts:
                    errors.append(f"Invalid department in register_number: {dept}")
                    is_valid = False
                if not num.isdigit():
                    errors.append(f"Non-numeric sequence in register_number: {num}")
                    is_valid = False
                if reg_num.lower() in register_numbers:
                    errors.append(f"Duplicate register_number: {reg_num}")
                    is_valid = False
                # Standardize department to uppercase for consistency
                row["register_number"] = f"{college_code}{year_batch}{dept.upper()}{num}"
                register_numbers.add(reg_num.lower())

            # Validate name
            if not re.match(name_pattern, row["name"].strip()):
                errors.append(f"Invalid characters in name: {row['name']}")
                is_valid = False

            # Validate email
            email = row["email"].strip()
            if not re.match(email_pattern, email):
                errors.append(f"Invalid email format: {email}")
                is_valid = False
            if email.lower() in emails:
                errors.append(f"Duplicate email: {email}")
                is_valid = False
            emails.add(email.lower())

            # Validate year field (allow Roman numerals or numeric)
            year_val = row["year"].strip()
            if year_val in roman_to_numeric:
                numeric_year = roman_to_numeric[year_val]
            else:
                try:
                    numeric_year = int(year_val)
                    if not 1 <= numeric_year <= 4:
                        errors.append(f"Invalid study year: {year_val}")
                        is_valid = False
                except ValueError:
                    errors.append(f"Invalid study year (must be I, II, III, IV or 1, 2, 3, 4): {year_val}")
                    is_valid = False

            # Validate section
            if not re.match(section_pattern, row["section"].strip()):
                errors.append(f"Invalid section format: {row['section']}")
                is_valid = False

            if is_valid:
                valid_rows.append(row)
            else:
                invalid_rows.append({"row": row, "errors": errors})

        return JsonResponse({
            "valid_rows": [dict(row) for row in valid_rows],
            "invalid_rows": [
                {
                    "row": dict(r["row"]),
                    "errors": r["errors"]
                } for r in invalid_rows
            ],
            "total_submitted": len(valid_rows) + len(invalid_rows),
            "valid_count": len(valid_rows),
            "invalid_count": len(invalid_rows)
        }, status=200)

    except csv.Error:
        return JsonResponse({"error": "Invalid CSV format"}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Server error: {str(e)}"}, status=500)

@csrf_exempt
@require_POST
def confirm_upload_students(request):
    """
    Accepts valid_rows from JSON payload, checks for duplicates in the database,
    and inserts non-duplicate rows into the student collection.
    Throws an error listing any duplicate register_numbers found.
    """
    try:
        data = json.loads(request.body)
        valid_rows = data.get('valid_rows', [])

        student_collection = db["student"]
        inserted = 0
        duplicates = []

        for row in valid_rows:
            register_number = row.get("register_number")
            if not register_number:
                continue

            # Check for existing register_number in the database
            if student_collection.find_one({"register_number": register_number}):
                duplicates.append(register_number)
                continue

            student = {
                "name": row.get("name"),
                "register_number": register_number,
                "college_name": row.get("college_name"),
                "department": row.get("department"),
                "year": row.get("year"),
                "section": row.get("section"),
                "email": row.get("email"),
                "batch": row.get("batch", ""),
            }

            student_collection.insert_one(student)
            inserted += 1

        # If duplicates were found, return an error with details
        if duplicates:
            return JsonResponse({
                "error": "Duplicate register_numbers found in the database",
                "duplicates": duplicates,
                "inserted": inserted
            }, status=400)

        return JsonResponse({
            "message": "Bulk upload complete",
            "inserted": inserted,
            "duplicates": []
        }, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Server error: {str(e)}"}, status=500)

@csrf_exempt
def update_student(request, student_id):
    """
    Updates a specific student's information by their ID.
    """
    print(f"Received request for update_student with student_id: {student_id}")  # Debugging
    if request.method != "PUT":
        print(f"Invalid method: {request.method}")  # Debugging
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        try:
            object_id = ObjectId(student_id)
            print(f"Converted student_id to ObjectId: {object_id}")  # Debugging
        except Exception as e:
            print(f"Error converting student_id to ObjectId: {str(e)}")  # Debugging
            return JsonResponse({"error": "Invalid student ID format"}, status=400)

        existing_student = student_collection.find_one({"_id": object_id})
        if not existing_student:
            print(f"No student found with ID: {object_id}")  # Debugging
            return JsonResponse({"error": "Student not found"}, status=404)

        # print(f"Found student: {existing_student}")  # Debugging
        data = json.loads(request.body)
        print(f"Received payload: {data}")  # Debugging

        update_data = {}
        if 'name' in data:
            update_data['name'] = data['name']
        if 'register_number' in data:
            if data['register_number'] != existing_student.get('register_number'):
                if student_collection.find_one({"register_number": data['register_number']}):
                    print(f"Register number {data['register_number']} already in use")  # Debugging
                    return JsonResponse({"error": "Register number already in use"}, status=409)
                update_data['register_number'] = data['register_number']
        if 'college_name' in data:
            update_data['college_name'] = data['college_name']
        if 'department' in data:
            update_data['department'] = data['department']
        if 'year' in data:
            update_data['year'] = data['year']
        if 'section' in data:
            update_data['section'] = data['section']
        if 'email' in data:
            update_data['email'] = data['email']

        update_data['updated_at'] = datetime.now()
        print(f"Update data to be applied: {update_data}")  # Debugging

        result = student_collection.update_one(
            {"_id": object_id},
            {"$set": update_data}
        )

        print(f"Update result: matched={result.matched_count}, modified={result.modified_count}")  # Debugging

        if result.modified_count == 0:
            print("No changes made to the student")  # Debugging
            return JsonResponse({"message": "No changes made to the student"}, status=200)

        print("Student updated successfully")  # Debugging
        return JsonResponse({"message": "Student updated successfully"}, status=200)

    except json.JSONDecodeError as e:
        print(f"JSON decode error: {str(e)}")  # Debugging
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        print(f"Unexpected error: {str(e)}")  # Debugging
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def delete_student(request, student_id):
    """
    Deletes a specific student by their ID if no answer sheets or results exist.
    Returns whether deletion is allowed based on report data.
    """
    print(f"Received request for delete_student with student_id: {student_id}")  # Debugging
    if request.method != "DELETE":
        print(f"Invalid method: {request.method}")  # Debugging
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        try:
            object_id = ObjectId(student_id)
            print(f"Converted student_id to ObjectId: {object_id}")  # Debugging
        except Exception as e:
            print(f"Error converting student_id to ObjectId: {str(e)}")  # Debugging
            return JsonResponse({"error": "Invalid student ID format"}, status=400)

        existing_student = student_collection.find_one({"_id": object_id})
        if not existing_student:
            print(f"No student found with ID: {object_id}")  # Debugging
            return JsonResponse({"error": "Student not found"}, status=404)

        # print(f"Found student: {existing_student}")  # Debugging

        # Check for answer sheets
        answer_sheets = answer_sheet_collection.find_one({"student_id": existing_student['register_number']})
        # Check for results
        results = results_collection.find_one({"student_id": existing_student['register_number']})

        if answer_sheets or results:
            print(f"Cannot delete student {existing_student['register_number']} due to existing answer sheets or results")  # Debugging
            return JsonResponse({
                "error": "Cannot delete student with submitted answer sheets or recorded results",
                "can_delete": False
            }, status=400)

        print(f"No report data found for student {existing_student['register_number']}, proceeding with deletion")  # Debugging
        result = student_collection.delete_one({"_id": object_id})

        if result.deleted_count == 0:
            print("Failed to delete student")  # Debugging
            return JsonResponse({"error": "Failed to delete student", "can_delete": True}, status=500)

        print("Student deleted successfully")  # Debugging
        return JsonResponse({"message": "Student deleted successfully", "can_delete": True}, status=200)

    except Exception as e:
        print(f"Unexpected error: {str(e)}")  # Debugging
        return JsonResponse({"error": str(e), "can_delete": False}, status=500)

@csrf_exempt
def toggle_student_status(request, student_id):
    """
    Toggles a student's status between Active and Inactive.
    """
    if request.method != "PATCH":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        try:
            object_id = ObjectId(student_id)
        except:
            return JsonResponse({"error": "Invalid student ID format"}, status=400)

        existing_student = student_collection.find_one({"_id": object_id})
        if not existing_student:
            return JsonResponse({"error": "Student not found"}, status=404)

        data = json.loads(request.body)
        new_status = data.get("status")

        if new_status not in ["Active", "Inactive"]:
            return JsonResponse({"error": "Invalid status. Must be 'Active' or 'Inactive'"}, status=400)

        result = student_collection.update_one(
            {"_id": object_id},
            {
                "$set": {
                    "status": new_status,
                    "updated_at": datetime.now()
                }
            }
        )

        if result.modified_count == 0:
            return JsonResponse({"message": "No changes made to the student status"}, status=200)

        return JsonResponse({"message": f"Student status updated to {new_status}"}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def get_student_semester_report(request, rollno=None, subject_code=None):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        if not rollno or not subject_code:
            return JsonResponse({"error": "Missing student roll number or subject code"}, status=400)

        # Get student info
        student_info = student_collection.find_one({"register_number": rollno})
        if not student_info:
            return JsonResponse({"error": f"Student not found: {rollno}"}, status=404)

        student_profile = {
            "register_number": rollno,
            "name": student_info.get("name", ""),
            "email": student_info.get("email", ""),
            "department": student_info.get("department", ""),
            "year": student_info.get("year", ""),
            "section": student_info.get("section", "")
        }

        # Find all results for this student and subject
        query = {
            "results.subjects.subject_code": subject_code,
            "results.subjects.students.register_number": rollno
        }

        result_docs = results_collection.find(query)
        exam_results = []

        # Placeholder to calculate overall summary
        total_marks_by_exam_type = {}
        total_count_by_exam_type = {}
        pass_count = 0
        fail_count = 0
        bloom_counter = {}
        co_counter = {}
        grade_distribution = {
            "O": 0,
            "A+": 0,
            "A": 0,
            "B+": 0,
            "B": 0,
            "Others": 0
        }

        ai_scores = []
        staff_scores = []
        ai_full_marks = []
        staff_full_marks = []

        def get_grade(marks):
            if marks >= 91: return "O"
            elif marks >= 81: return "A+"
            elif marks >= 71: return "A"
            elif marks >= 61: return "B+"
            elif marks >= 50: return "B"
            else: return "Others"

        for result in result_docs:
            exam_type = result.get("exam_type", "Unknown")
            for subject in result.get("results", {}).get("subjects", []):
                if subject.get("subject_code") != subject_code:
                    continue

                for student in subject.get("students", []):
                    if student.get("register_number") != rollno:
                        continue

                    answers = []
                    for ans in student.get("evaluated_answers", []):
                        bloom = ans.get("bloom_level")
                        co = ans.get("co")
                        if bloom:
                            bloom_counter[bloom] = bloom_counter.get(bloom, 0) + 1
                        if co:
                            co_counter[co] = co_counter.get(co, 0) + 1
                        answers.append(ans)

                    ai_mark = student.get("total_marks", 0)
                    staff_mark = student.get("staff_mark", 0)

                    max_mark = 50 if "IAE" in exam_type.upper() else 100
                    ai_scores.append(ai_mark)
                    staff_scores.append(staff_mark)
                    ai_full_marks.append(max_mark)
                    staff_full_marks.append(max_mark)

                    grade = get_grade(ai_mark)
                    grade_distribution[grade] += 1

                    if ai_mark >= 50:
                        pass_count += 1
                    else:
                        fail_count += 1

                    if exam_type not in total_marks_by_exam_type:
                        total_marks_by_exam_type[exam_type] = 0
                        total_count_by_exam_type[exam_type] = 0

                    total_marks_by_exam_type[exam_type] += ai_mark
                    total_count_by_exam_type[exam_type] += 1

                    exam_results.append({
                        "exam_type": exam_type,
                        "total_marks": ai_mark,
                        "staff_mark": staff_mark,
                        "questions_answered": len(answers),
                        "answers": answers
                    })

        # Fetch subject_name from results collection
        subject_name = ""
        sample_result = results_collection.find_one({
            "results.subjects.subject_code": subject_code,
            "results.subjects.students.register_number": rollno
        })
        if sample_result:
            for subj in sample_result.get("results", {}).get("subjects", []):
                if subj.get("subject_code") == subject_code:
                    subject_name = subj.get("subject_name", "")
                    break

        ai_avg = round(sum(ai_scores) / 4, 2) if ai_scores else 0
        staff_avg = round(sum(staff_scores) / 4, 2) if staff_scores else 0

        overall = {
            "iae_vs_semester": { 
                et: round(total_marks_by_exam_type[et] / total_count_by_exam_type[et], 2)
                for et in total_marks_by_exam_type
            },
            "grade_distribution": grade_distribution,
            "pass_percentage": {
                "pass": pass_count,
                "fail": fail_count,
                "total": pass_count + fail_count
            },
            "bloom_performance": bloom_counter,
            "co_distribution": co_counter,
            "ai_vs_manual_discrepancy": {
                "ai_avg": ai_avg,
                "staff_avg": staff_avg,
            }
        }

        return JsonResponse({
            "student": student_profile,
            "subject_code": subject_code,
            "subject_name": subject_name or subject_code,
            "overall": overall,
            "exam_results": exam_results
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
