# Odysseus Study Feature Backend Analysis

Technical analysis of the HTTP/route and orchestration layer for the Study feature in Odysseus.

## 1. Endpoint Inventory

### Decks Management
- `GET /api/study/decks` - List decks with counts
- `POST /api/study/decks` - Create a new deck
- `PUT /api/study/decks/{deck_id}` - Update deck properties
- `DELETE /api/study/decks/{deck_id}` - Delete deck and associated data

### Flashcards (StudyCard)
- `GET /api/study/decks/{deck_id}/cards` - List cards in a deck
- `POST /api/study/decks/{deck_id}/cards` - Create new cards
- `PUT /api/study/cards/{card_id}` - Update a card
- `DELETE /api/study/cards/{card_id}` - Delete a card

### Review Queue & Scheduling
- `GET /api/study/queue` - Get FSRS review queue (due + capped new)
- `POST /api/study/cards/{card_id}/review` - Apply rating and schedule next review

### AI Generation
- `POST /api/study/ai/generate-cards` - Generate flashcards from text
- `POST /api/study/ai/quiz` - Generate quiz questions
- `POST /api/study/ai/grade` - Grade free-recall answers

### Exams & Study Plans
- `GET /api/study/exams` - List exams
- `POST /api/study/exams` - Create exam
- `PUT /api/study/exams/{exam_id}` - Update exam
- `DELETE /api/study/exams/{exam_id}` - Delete exam
- `POST /api/study/exams/{exam_id}/generate-plan` - Generate study plan
- `POST /api/study/exams/{exam_id}/toggle-block` - Toggle plan block completion

### Focus Timer Sessions
- `POST /api/study/focus/start` - Start focus session
- `POST /api/study/focus/{session_id}/finish` - Finish focus session
- `GET /api/study/focus/recent` - Get recent focus sessions

### Study Materials/Notes
- `GET /api/study/decks/{deck_id}/materials` - List materials
- `POST /api/study/decks/{deck_id}/materials` - Create/upload material
- `DELETE /api/study/materials/{material_id}` - Delete material
- `PUT /api/study/materials/{material_id}/category` - Set material category
- `POST /api/study/materials/{material_id}/reextract-text` - Re-extract text from file
- `GET /api/study/figures/{material_id}/{idx}` - Serve extracted figure
- `GET /api/study/materials/{material_id}/notes` - Get material notes
- `POST /api/study/materials/{material_id}/notes` - Generate material notes
- `GET /api/study/decks/{deck_id}/overview` - Get deck overview
- `POST /api/study/decks/{deck_id}/overview` - Generate deck overview
- `POST /api/study/materials/{material_id}/extract` - Extract questions from material

### Question Bank
- `GET /api/study/decks/{deck_id}/questions` - List questions
- `PUT /api/study/questions/{question_id}` - Update question
- `DELETE /api/study/questions/{question_id}` - Delete question

### Practice Mode
- `GET /api/study/practice/queue` - Get practice question queue
- `POST /api/study/questions/{question_id}/attempt` - Submit answer attempt
- `POST /api/study/questions/{question_id}/hint` - Get hint for question
- `POST /api/study/questions/{question_id}/ask` - Ask AI about question
- `POST /api/study/questions/{question_id}/explain` - Get MCQ explanation
- `POST /api/study/questions/{question_id}/explain-further` - Get detailed explanation

### Consultation Features
- `POST /api/study/questions/{question_id}/locate` - Locate source material
- `POST /api/study/cards/{card_id}/explain-further` - Get detailed card explanation

### Dashboard & Statistics
- `GET /api/study/overview` - Get study dashboard overview
- `GET /api/study/stats` - Get study statistics
- `GET /api/study/history` - Get review/attempt history

### Maintenance Tools
- `POST /api/study/reformat` - Reformat questions/cards to LaTeX
- `POST /api/study/decks/{deck_id}/link-parts` - Link multi-part questions
- `POST /api/study/dedup` - Deduplicate questions
- `POST /api/study/backfill-context` - Backfill multi-part question context
- `POST /api/study/audit-questions` - Audit questions for solution leaks
- `GET /api/study/questions/{question_id}/prereqs` - Get prerequisite questions

## 2. Data Model

The Study feature uses several SQLAlchemy models stored in the `core/database.py` file:

### StudyDeck
- `id`: Primary key
- `owner`: Username
- `name`: Deck name
- `description`: Optional description
- `color`: Visual color coding
- `archived`: Archive flag
- `new_per_day`: Daily new card introduction limit
- `retention`: Desired FSRS retention rate (stored as string)
- `overview`: Cached AI-generated subject overview

### StudyCard
- `id`: Primary key
- `owner`: Username
- `deck_id`: Foreign key to StudyDeck
- `front`: Front of flashcard (question/prompt)
- `back`: Back of flashcard (answer)
- `notes`: Optional extra context
- `tags`: JSON list of tags
- `suspended`: Suspension flag
- `source`: Source of card ("user" or "ai")
- `deep_explanation`: Cached detailed explanation with source links

FSRS scheduling fields:
- `state`: Card state ("new", "learning", "review", "relearning")
- `stability`: Float as string for SQLite compatibility
- `difficulty`: Float as string
- `due`: Next review datetime
- `last_review`: Last review datetime
- `reps`: Number of repetitions
- `lapses`: Number of lapses

### StudyReview
Append-only log of card reviews:
- `id`: Primary key
- `owner`: Username
- `card_id`: Card being reviewed
- `deck_id`: Deck ID
- `rating`: FSRS rating (1-4)
- `state_before`: State before review
- `interval_days`: Granted interval in days
- `duration_ms`: Time spent reviewing
- `reviewed_at`: Review timestamp

### StudyExam
- `id`: Primary key
- `owner`: Username
- `title`: Exam title
- `exam_date`: ISO date string
- `exam_format`: Format description
- `hours_per_week`: Study hours per week
- `rest_days`: JSON list of rest days
- `topics`: JSON list of topics with importance/mastery
- `plan`: Generated study plan JSON
- `done_blocks`: Completed plan blocks
- `archived`: Archive flag

### StudyFocusSession
- `id`: Primary key
- `owner`: Username
- `label`: Description of study session
- `planned_min`: Planned duration in minutes
- `actual_min`: Actual duration
- `started_at`: Start timestamp
- `ended_at`: End timestamp
- `completed`: Completion flag

### StudyMaterial
- `id`: Primary key
- `owner`: Username
- `deck_id`: Foreign key to StudyDeck
- `name`: Material name
- `kind`: Type ("text", "pdf", "file")
- `file_id`: Uploaded file ID
- `content`: Extracted text content
- `char_count`: Character count
- `question_count`: Number of extracted questions
- `summary`: AI-generated study notes
- `category`: Material category ("theory" or "exam")

### StudyQuestion
Practice questions in the question bank:
- `id`: Primary key
- `owner`: Username
- `deck_id`: Foreign key to StudyDeck
- `material_id`: Source material
- `qtype`: Question type ("mcq" or "open")
- `question`: Question text
- `context`: Shared problem setup for multi-part questions
- `options`: JSON list of MCQ options
- `correct_index`: Correct option index for MCQ
- `reference`: Reference answer/solution
- `explanation`: Cached MCQ explanation
- `deep_explanation`: Cached detailed explanation with source links
- `number`: Source question number/part label
- `source_page`: Page number in source material
- `prereq_ids`: JSON list of prerequisite question IDs
- `topic`: Topic label
- `difficulty`: Difficulty level ("easy", "medium", "hard")
- `origin`: Origin of question ("extracted", "authored", "user")
- `suspended`: Suspension flag

FSRS scheduling fields (same as StudyCard):
- `state`: Question state
- `stability`: Float as string
- `fsrs_difficulty`: Float as string
- `due`: Next review datetime
- `last_review`: Last review datetime
- `reps`: Number of repetitions
- `lapses`: Number of lapses

### StudyAttempt
Append-only log of question attempts:
- `id`: Primary key
- `owner`: Username
- `question_id`: Question being attempted
- `deck_id`: Deck ID
- `qtype`: Question type
- `answer`: Given answer
- `correct`: MCQ correctness
- `score`: Open question score (0-100)
- `rating`: Applied FSRS rating
- `confidence`: Confidence level ("sure", "unsure", "guess")
- `hints_used`: Number of hints used
- `grading`: JSON grading payload for open questions
- `duration_ms`: Time spent on attempt
- `attempted_at`: Attempt timestamp

## 3. AI/LLM Orchestration

### Model Resolution
The system uses `_resolve_study_model()` to determine which LLM endpoint to use:
1. First tries the "study" endpoint with user's configuration
2. Falls back to "utility" endpoint
3. Finally falls back to "default" endpoint
4. Optionally uses a separate "study_text_model" for text-only operations

### Text Processing Pipeline
- `_llm_json()`: One-shot LLM call returning parsed JSON with automatic JSON repair
- `_llm_text()`: One-shot LLM call returning plain text
- `_repair_llm_json()`: Attempts to repair malformed JSON responses

### Question Extraction
Two modes available:
1. **Extract**: Pull questions from past papers/problem sets
2. **Author**: Generate new questions from notes/theory

Process:
1. Discovery pass identifies question locations and answer key pages
2. Main extraction pass transcribes questions with context
3. Coverage analysis compares discovered vs extracted questions
4. Targeted re-extraction for missing questions
5. Normalization and deduplication of results

### Vision-Based Extraction
Supports PDF processing with vision models:
- Renders PDF pages to images
- Uses vision models to extract questions from scanned/formula-heavy PDFs
- Automatic fallback between text and vision extraction modes

### Grading and Feedback
- MCQ questions are graded locally based on correct answer
- Open-ended questions are graded by LLM using reference answers
- Detailed feedback and follow-up questions provided
- Confidence ratings influence FSRS scheduling

### Explanation Systems
Multiple explanation systems for different contexts:
- `EXPLAIN_SYSTEM`: Brief MCQ explanations
- `EXPLAIN_FURTHER_SYSTEM`: Detailed explanations grounded in course materials
- `ASK_COACH_SYSTEM`: Socratic guidance during attempts
- `ASK_TUTOR_SYSTEM`: Detailed tutoring after submission

## 4. Review/Scheduling Flow

### FSRS Implementation
Uses FSRS-4.5 algorithm implemented in `src/fsrs.py`:
- Four rating levels: Again (1), Hard (2), Good (3), Easy (4)
- Four card states: new, learning, review, relearning
- Stability and difficulty tracking per card/question
- Configurable retention targets per deck

### Card Review Process
1. User rates card via `POST /api/study/cards/{card_id}/review`
2. Rating is processed through FSRS scheduler
3. New scheduling parameters calculated
4. Review logged in StudyReview table
5. Card updated with new scheduling state

### Practice Question Queue
- Due questions prioritized by due date
- New questions interleaved across topics
- Configurable limits on new introductions per day
- Multi-part question prerequisite handling

### Rating Mapping Logic
Intelligent mapping from practice outcomes to FSRS ratings:
- Failures always map to "Again"
- Success with hints maps to "Hard"
- Clean success maps to "Good" or "Easy" based on confidence/quality

## 5. Code Quality Issues

### Complexity Hotspots
1. **Massive route file**: `routes/study_routes.py` has 3,553 lines making it difficult to navigate
2. **Complex extraction logic**: Question extraction involves multiple passes, fallbacks, and recovery mechanisms
3. **Interleaved business logic and HTTP handling**: Database operations mixed with request/response handling

### Duplication
1. **Repeated DB patterns**: Similar database query patterns repeated throughout
2. **JSON handling**: Multiple implementations of JSON parsing and error handling
3. **Authentication checks**: Owner verification duplicated across many endpoints

### Error Handling Gaps
1. **Inconsistent error responses**: Mix of HTTP exceptions and manual error responses
2. **Missing transaction handling**: Some operations lack proper rollback mechanisms
3. **Partial failure handling**: Complex operations may leave data in inconsistent states

### Security Concerns
1. **Path traversal vulnerability**: `_resolve_uploaded_file()` function has potential path traversal issues
2. **Insufficient authorization checks**: Some endpoints may lack proper owner verification
3. **Input validation**: Limited validation on user-provided data

### Performance Issues
1. **N+1 Query Problems**: Several endpoints exhibit classic N+1 query patterns
2. **Sync DB in async handlers**: Blocking database operations in async request handlers
3. **Inefficient data loading**: Loading entire datasets when only subsets are needed

### Technical Debt
1. **TODO comments**: Several places marked with TODO comments indicating unfinished work
2. **Legacy code patterns**: Some code follows older patterns that could be modernized
3. **Stringly-typed parameters**: Heavy reliance on string manipulation for data handling

## 6. Improvement Opportunities

### Immediate Priorities
1. **Route modularization**: Split the massive study_routes.py into logical modules
2. **Database optimization**: Implement proper connection pooling and async database operations
3. **Security hardening**: Address path traversal vulnerabilities and strengthen auth checks

### Medium-term Improvements
1. **Performance optimization**: Eliminate N+1 queries and optimize data loading
2. **Error handling standardization**: Implement consistent error response patterns
3. **Transaction management**: Add proper transaction boundaries for complex operations

### Long-term Architecture
1. **Service layer separation**: Extract business logic into dedicated service classes
2. **Caching strategy**: Implement intelligent caching for expensive operations
3. **Background job processing**: Move long-running operations to background workers
4. **API versioning**: Introduce proper API versioning for future evolution

### Specific Technical Improvements
1. **Replace string-based scheduling state with enums**
2. **Implement proper pagination for list endpoints**
3. **Add input validation using Pydantic models**
4. **Introduce dependency injection for better testability**
5. **Add comprehensive logging and monitoring**
6. **Implement proper rate limiting for AI endpoints**
7. **Add database indexes for frequently queried fields**
8. **Refactor JSON handling to use consistent library functions**

## Conclusion

The Study feature backend implements a sophisticated spaced repetition system with AI-powered question extraction and generation. While powerful, the implementation suffers from code organization issues, security vulnerabilities, and performance problems that need addressing. The modularization of routes, optimization of database access patterns, and strengthening of security measures should be the immediate priorities for improvement.