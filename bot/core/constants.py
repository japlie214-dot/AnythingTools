# bot/core/constants.py
"""Centralized constants for tool names using Azure-compatible underscore naming.
All tool names are defined here to ensure consistency across the codebase.
"""

# System tools
TOOL_SYSTEM_INITIALIZE_CHECKLIST = "system_initialize_checklist"
TOOL_SYSTEM_COMPLETE_STEP = "system_complete_step"
TOOL_SYSTEM_SWITCH_MODE = "system_switch_mode"
TOOL_SYSTEM_SKILL_LIST = "system_skill_list"
TOOL_SYSTEM_SKILL_READ = "system_skill_read"
TOOL_SYSTEM_SKILL_CRUD = "system_skill_crud"
TOOL_SYSTEM_FILE_LIST_DOWNLOADS = "system_file_list_downloads"
TOOL_SYSTEM_FILE_READ_DOCUMENT = "system_file_read_document"
TOOL_SYSTEM_FILE_DELETE = "system_file_delete"

# Library tools
TOOL_LIBRARY_VECTOR_SEARCH = "library_vector_search"
TOOL_LIBRARY_GET_PDF_TOC = "library_get_pdf_toc"
TOOL_LIBRARY_PDF_SEARCH = "library_pdf_search"

# Browser tools
TOOL_BROWSER_MACRO_SAVE = "browser_macro_save"
TOOL_BROWSER_MACRO_EDIT = "browser_macro_edit"
TOOL_BROWSER_MACRO_DELETE = "browser_macro_delete"
TOOL_BROWSER_MACRO_EXECUTE = "browser_macro_execute"
TOOL_BROWSER_OPERATOR = "browser_operator"
