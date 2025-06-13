from django.urls import path
from .views import *
from .admins import *
from .subadmin import *
from .superadmin import *
from .Search import *
from .students import *
from .department import *

urlpatterns = [
    # Authentication
    path('signup/', admin_signup, name='admin_signup'),
    path('signin/', admin_signin, name='admin_signin'),
    path('set-password/', set_password, name='set_password'),
    path('send-verification-code/', send_verification_code, name='send_verification_code'),
    path('verify-code-reset-password/', verify_code_and_reset_password, name='verify_code_reset_password'),
    path('subadmin/signin/', subadmin_signin, name='subadmin_signin'),
    path('subadmin/set-password/', set_password_for_subadmin, name='set_password_for_subadmin'),
    path('subadmin/setup-password/', validate_password_setup_token, name='validate_password_setup_token_subadmin'),
    path('subadmin/verify-code-reset-password/', subadmin_verify_code_and_reset_password, name='verify_code_and_reset_password_subadmin'),
    path('subadmin/send-verification-code/', subadmin_send_verification_code, name='send_verification_code_subadmin'),
    path('superadmin/signin/', superadmin_signin, name='superadmin_signin'),
    path('superadmin/signup/', superadmin_signup, name='superadmin_signup'),
    path('superadmin/verify-code-reset-password/', superadmin_verify_code_and_reset_password, name='verify_code_and_reset_password_superadmin'),
    path('superadmin/send-verification-code/', superadmin_send_verification_code, name='send_verification_code_superadmin'),
    path('send-reset-link/', send_reset_link, name='send_reset_link'),
    path('reset-password/', reset_password, name='reset_password'),
    path('subadmin/send-reset-link/', subadmin_send_reset_link, name='send_reset_link'),
    path('subadmin/reset-password/', subadmin_reset_password, name='reset_password'),
    path('superadmin/send-reset-link/', superadmin_send_reset_link, name='send_reset_link_superadmin'),
    path('superadmin/reset-password/', superadmin_reset_password, name='reset_password_superadmin'),
    path('subadmin/send-setup-email/', send_subadmin_setup_email, name='send_subadmin_setup_email'),

    # Student Management
    path('get-students/', get_all_students, name='get_all_students'),
    path('add-student/', add_student, name='add_student'),
    path('preview-upload-students/', preview_upload_students, name='preview_upload_students'),
    path('confirm-upload-students/', confirm_upload_students, name='confirm_upload_students'),
    path('update-student/<str:student_id>/', update_student, name='update_student'),
    path('delete-student/<str:student_id>/', delete_student, name='delete_student'),
    path('toggle-student-status/<str:student_id>/', toggle_student_status, name='toggle_student_status'),
    path('toggle-subadmin-status/<str:subadmin_id>/', toggle_subadmin_status, name='toggle_subadmin_status'),
    path('student-semester-report/<str:rollno>/<str:subject_code>/', get_student_semester_report, name='get_student_semester_report'),

    # Subadmin Management
    path('toggle-subadmin-status/<str:subadmin_id>/', toggle_subadmin_status, name='toggle_subadmin_status'),

    # Exams Creation
    path('search-exams/', search_or_create_exam, name='search_exams'),
    path('semester-details/', submit_semester_details, name='submit_semester_details'),
    path('append-subject/', append_subjects_to_exam, name='append_subjects_to_exam'),
    path('append-subject-without-files/', append_subjects_to_exam_without_files, name='append_subjects_to_exam_without_files'),
    path('edit-subject/', edit_subjects_to_exam_without_files, name='edit_subjects_to_exam_without_files'),
    path('delete-subject/', delete_subjects_to_exam_without_files, name='edit_subjects_to_exam_without_files'),
    path('get-exam-subjects/', get_exam_subjects, name='get_exam_subjects'),
    path('exams/', get_all_exams, name='get_all_exams'),

    path('get-exam/<str:exam_id>/', get_exam_detail_by_id, name='get_exam_detail_by_id'),
    path('exam/<str:exam_id>/delete/', delete_exam, name='delete_exam'),
    path('cleanup-incomplete-exams/', cleanup_incomplete_exams, name='cleanup_incomplete_exams'),

    # Answer Sheet Management
    path('upload-answer-sheet/', upload_answer_sheet, name='upload_answer_sheet'),
    path('bulk-upload_answer/', bulk_upload_answer_sheets, name='bulk_upload_answer_sheets'),
    path('confirm-bulk-upload/', confirm_bulk_upload, name='confirm_bulk_upload'),
    path('bulk-upload-status/<str:job_id>/', get_bulk_upload_status, name='get_bulk_upload_status'),
    #path('start-bulk-evaluation/', start_bulk_evaluation, name='start_bulk_evaluation'),
    path('evaluation-status/<str:job_id>/', get_evaluation_status, name='get_evaluation_status'),
    path('view-answer-sheet/<str:filename>/', view_answer_sheet, name='view_answer_sheet'),
    path('list-answer-sheets/', list_answer_sheets, name='list_answer_sheets'),
    path('extract-handwritten-answers/', extract_handwritten_answers, name='extract_handwritten_answers'),
    path('process-and-evaluate-answer-sheet/', process_and_evaluate_answer_sheet, name='process_and_evaluate_answer_sheet'),
    path('validate-all/', validate_all_answer_sheets, name='validate_all'),
    path('job-status/<str:job_id>/', job_status, name='job_status'),
    path('revert-answer-sheet/', revert_answer_sheet, name='revert_answer_sheet'),
    path('student/<str:roll_number>/', get_student_by_roll_number, name='get_student_by_roll_number'),
    # path('submit_answers/', submit_answers, name='submit_answers'),
    path('get_data_in_results/', get_data_in_results, name='get_data_in_results'),
    path('get-exam-details-report/', get_exam_details_report, name='get_exam_details_by_ids'),
    path('get-iae-details-by-ids/', get_iae_details_by_ids, name='get_iae_details_by_ids'),  
    path('exam-analysis/', exam_analysis, name='exam_analysis'),
    path('student-analysis/', student_analysis, name='student_analysis'),
    path('get-exam-questions/<str:exam_id>/<str:subject_code>/', get_exam_questions, name='get_exam_questions'),
    
    #rubric management
    path('update-subject-rubrics/', update_subject_rubrics, name='update_subject_rubrics'),
    path('create-rubric/', create_rubric, name='create_rubric'),
    path('get-rubrics/', get_rubrics, name='get_rubrics'),
    path('update-rubric/', update_rubric, name='update_rubric'),
    path('delete-rubric/', delete_rubric, name='delete_rubric'),

    # Subject Management
    path('get-subject/<str:exam_id>/<str:subject_code>/', get_subject_by_id, name='get_exam_subject_by_code'),
    path('exam/<str:exam_id>/subject/<str:subject_code>/delete/', delete_subject_from_exam, name='delete_subject_from_exam'),
    path('search-or-create-subject/', search_or_create_subject, name='search_or_create_subject'),

    # Reports
    path('students-exam-details/', get_students_by_exam_details, name='get_students_by_exam_details'),
    path('get_exam_results/<str:exam_id>/', get_exam_results, name='get_exam_results'),
    path("question-breakdown/<str:exam_id>/<str:subject_code>/<str:register_number>", student_exam_report, name="student_exam_report"),
    path('get-student-results/<str:register_number>/', get_student_results, name='get_student_results'),
    path('update-staff-mark/', update_staff_mark, name='update_staff_mark'),
    path('results/average/', calculate_question_averages, name='calculate_question_averages'),
    path('upload-staff-marks/', upload_staff_marks, name='upload_staff_marks'),
    

    # Department routes
    path('create-department/', create_department, name='create_department'),
    path('edit-department/<str:dept_id>/', edit_department, name='edit_department'),
    path('delete-department/<str:dept_id>/', delete_department, name='delete_department'),
    path('assign-subject-to-department/', assign_subject_to_department, name='assign_subject_to_department'),
    path('bulk-upload-subjects/', bulk_upload_subjects, name='bulk_upload_subjects'),
    path('get-departments/', get_departments_by_college, name='get_departments_by_college'),
    path('get-department/<str:dept_id>/', get_department_by_id, name='get_department_by_id'),
    path('get-department-student/<str:dept_id>/', get_department_student, name='get_department_student'),
    path('get-unique-batches/', get_unique_batches, name='get_unique_batches'),
    path('edit-subject/', edit_subject, name='edit_subject'),
    path('delete-subject/', delete_subject, name='delete_subject'),

    # Superadmin routes
    path('superadmin/get-all-admins/', get_all_admins, name='get_all_admins'),
    path('superadmin/get-admin/', get_admin_by_id, name='get_admin_by_id'),
    path('superadmin/toggle-admin-status/', toggle_admin_status, name='toggle_admin_status'),
    path('subadmin/create/', create_subadmin, name='create_subadmin'),
    path('subadmin/list/', get_all_subadmins, name='get_all_subadmins'),
    path('subadmin/<str:subadmin_id>/', get_subadmin_by_id, name='get_subadmin_by_id'),
    path('subadmin/<str:subadmin_id>/update/', update_subadmin, name='update_subadmin'),
    path('subadmin/<str:subadmin_id>/delete/', delete_subadmin, name='delete_subadmin'),
    path('subadmin/signup/', subadmin_signup, name='subadmin_signup'),

    # Search
    path('search-all-collections/', search_data_in_all_collections, name='search_all_collections'),
    path('admin/search-collections/', search_data_for_admin, name='search_admin_collections'),
    
    #Subject Stats
    path('exam-statistics/<str:exam_id>/<str:subject_code>/', calculate_exam_statistics, name='exam_statistics_detail'),
    
    #get exam details 
    path('sem_exam_details/<str:exam_id>/<str:subject_code>/', get_sem_exam_details, name='get_exam_details'),
    
    #get ia based exam details
    path("sem_exam_performance/<str:exam_id>/<str:subject_code>/<str:exam_type>", get_sem_exam_performance, name="exam_performance"),
    
    #get semester details stats
    path('exam-analysis/<str:exam_id>/<str:subject_code>/', exam_analysis, name='exam_analysis'),
    
    #get student analysis with marks 
    path('student-analysis-marks/', get_students_with_detailed_results, name='student_analysis_marks'),
    
   
    path('student-exam-details/<str:exam_id>/<str:subject_code>/<str:register_number>/', get_student_exam_details, name='get_student_exam_details'),


]
