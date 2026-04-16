# tools/quiz/tool.py
"""
Quiz Generation Tool - Educational Assessment System

Generates Telegram-compatible quiz questions from educational content with:
- Topic extraction from context/titles
- Multi-question generation with validation
- Truncation to Telegram API limits
- Bilingual support (EN → ID)
- Structured JSON outputs (eliminating manual retry loops)
"""

import json
import logging
import asyncio
from typing import Any, List

from clients.llm import get_llm_client, LLMRequest
from tools.base import BaseTool
from utils.text_processing import parse_llm_json
import config
from utils.logger import get_dual_logger
from api.telegram_client import TelegramBot

log = get_dual_logger(__name__)
from tools.quiz.quiz_prompts import TRANSLATION_PROMPT, prepare_quiz_for_translation, QUIZ_TOPIC_EXTRACTION_PROMPT, QUIZ_GENERATION_PROMPT


# Import config
try:
    from config import QUIZ_MAX_QUESTIONS
except ImportError:
    QUIZ_MAX_QUESTIONS = 5

logger = logging.getLogger(__name__)

# Telegram poll API hard limits
POLL_QUESTION_LIMIT    = 300   # Telegram Bot API hard limit
POLL_OPTION_LIMIT      = 100   # Per option
POLL_EXPLANATION_LIMIT = 200   # Explanation text


def sanitize_quiz_question(q: dict) -> dict:
    """
    Enforce Telegram poll API character limits on a single quiz question.
    Mutates and returns a copy — never modifies the original dict.

    Expected dict shape:
        {'q': str, 'options': list[str], 'correct': int, 'exp': str}
    """
    q = dict(q)   # shallow copy

    if len(q.get('q', '')) > POLL_QUESTION_LIMIT:
        q['q'] = q['q'][:POLL_QUESTION_LIMIT - 3] + '...'

    if 'options' in q:
        q['options'] = [
            opt[:POLL_OPTION_LIMIT - 3] + '...'
            if len(opt) > POLL_OPTION_LIMIT else opt
            for opt in q['options']
        ]

    if len(q.get('exp', '')) > POLL_EXPLANATION_LIMIT:
        q['exp'] = q['exp'][:POLL_EXPLANATION_LIMIT - 3] + '...'

    return q


class QuizTool(BaseTool):
    """
    Quiz Tool that generates educational assessments from topic inputs.
    
    Input arguments:
        titles (list[str], optional): List of topic titles
        topic_context (str, optional): Free-form text about topics
    """
    
    name = "quiz"

    async def _deliver_polls(
        self,
        validated_quizzes: list[dict],
        telemetry,
    ) -> int:
        """
        Deliver validated quiz questions as native Telegram Quiz Polls.
        Returns count of polls successfully sent.
        """
        sent_count = 0
        for i, q in enumerate(validated_quizzes):
            question    = q['q'][:300]
            options     = [o[:100] for o in q['options'][:10]]
            correct     = int(q.get('correct', 0))
            explanation = q.get('exp', '')[:200]

            # Minimum 2 options required by Telegram API
            if len(options) < 2:
                log.dual_log(
                    tag="Quiz:Poll",
                    message=f"Poll Q{i+1} has fewer than 2 options. Skipping.",
                    level="WARNING",
                )
                continue

            # correct index must be within range
            if correct >= len(options):
                correct = 0

            try:
                await TelegramBot.send_poll(
                    question=question,
                    options=options,
                    correct_option_id=correct,
                    explanation=explanation,
                )
                sent_count += 1
                await telemetry(self.status(f'Poll {i+1}/{len(validated_quizzes)} sent.', 'RUNNING'))
            except Exception as e:
                log.dual_log(
                    tag="Quiz:Poll",
                    message=f"Failed to send poll Q{i+1}: {e}",
                    level="ERROR",
                    exc_info=e,
                )
            finally:
                await asyncio.sleep(getattr(config, "TELEGRAM_MESSAGE_DELAY", 1.0))

        return sent_count

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute quiz generation pipeline."""
        # ── DRY_RUN guard ───────────────────────────────────────────
        dry_run = kwargs.get('dry_run', config.TELEGRAM_DRY_RUN)
        if dry_run:
            log.dual_log(
                tag="Quiz",
                message=f"[DRY RUN] Would generate quiz for topics: {args.get('titles', [])}",
            )
            return "[DRY RUN] Quiz generation skipped."

        # Resolve input arguments
        titles = args.get("titles", [])
        topic_context = args.get("topic_context", "") or ", ".join(titles) or "Recent financial market trends"
        
        # Initialize LLM
        llm = get_llm_client(provider_type="azure")
        
        # ── Step 1: Topic Extraction ──
        await telemetry(self.status("Extracting foundational topics...", "RUNNING"))
        
        from utils.text_processing import escape_prompt_separators
        extract_prompt = QUIZ_TOPIC_EXTRACTION_PROMPT.format(
            topic_context=escape_prompt_separators(topic_context)
        )
        
        topics_resp = await llm.complete_chat(LLMRequest(
            messages=[{"role": "user", "content": extract_prompt}]
        ))
        topics = topics_resp.content.strip()
        
        # ── Step 2: Question Generation with Structured JSON ──
        await telemetry(self.status("Generating quiz questions...", "RUNNING"))
        
        from utils.text_processing import escape_prompt_separators
        quiz_prompt = QUIZ_GENERATION_PROMPT.format(
            topics=escape_prompt_separators(topics),
            max_questions=QUIZ_MAX_QUESTIONS
        )

        
        # Use structured JSON output format
        quiz_resp = await llm.complete_chat(LLMRequest(
            messages=[{"role": "user", "content": quiz_prompt}],
            response_format={"type": "json_object"}
        ))
        
        # ── Step 3: Parse Structured JSON ──
        quiz_data = parse_llm_json(quiz_resp.content)
        quizzes = quiz_data.get("questions", [])
        log.dual_log(
            tag="Quiz:Generation",
            message=f"Generated {len(quizzes)} question(s).",
            payload=quizzes,
        )
        
        if not quizzes:
            await telemetry(self.status("No valid quizzes generated or parse failed", "ERROR"))
            return f"### ❌ Quiz Failed\nNo valid questions were generated.\nRaw: {quiz_resp.content[:500]}"
        
        # ── Step 4: Validate & Truncate ──
        await telemetry(self.status("Validating quiz format...", "RUNNING"))
        final_en = ["### 🎓 Quiz — English\n"]
        validated_quizzes = []
        
        for idx, q in enumerate(quizzes[:QUIZ_MAX_QUESTIONS]):
            # Use the standardized sanitization utility (G5)
            validated_q = sanitize_quiz_question(q)
            validated_quizzes.append(validated_q)
            
            # Build English output
            final_en.append(f"**Q{idx+1}:** {validated_q['q']}")
            final_en.append(f"  Options: {' | '.join(validated_q['options'])}")
            final_en.append(f"  ✅ Correct: Option #{validated_q['correct'] + 1}")
            final_en.append(f"  💡 {validated_q['exp']}\n")
        
        # Deliver native Telegram quiz polls using centralized TelegramBot
        await telemetry(self.status('Sending quiz polls to Telegram...', 'RUNNING'))
        polls_sent = await self._deliver_polls(validated_quizzes, telemetry)
        await telemetry(self.status(f'{polls_sent} quiz polls delivered.', 'SUCCESS'))

        # ── Step 5: Bilingual Translation with Structural Isolation ──
        await telemetry(self.status("Translating to Bahasa Indonesia...", "RUNNING"))
        
        # Prepare text with structural isolation tags
        tagged_text = prepare_quiz_for_translation({"questions": validated_quizzes[:QUIZ_MAX_QUESTIONS]})
        
        from utils.text_processing import escape_prompt_separators
        translate_prompt = TRANSLATION_PROMPT + "\n" + escape_prompt_separators(tagged_text) + "\n###"
        
        trans_resp = await llm.complete_chat(LLMRequest(
            messages=[{"role": "user", "content": translate_prompt}],
            model=config.AZURE_TRANSLATOR_DEPLOYMENT
            # Note: We do NOT use json_object response format here to prevent LLM from breaking tags
        ))
        
        final_id = ["\n### 🇮🇩 Kuis — Bahasa Indonesia\n"]
        
        try:
            # Parse the translated text back into structured data
            # The response should maintain the "Option X: <t>...</t>" format
            translated_lines = trans_resp.content.strip().split('\n')
            
            trans_qs = []
            current_question = None
            current_options = []
            current_exp = None
            
            import re
            for line in translated_lines:
                line = line.strip()
                if not line: continue
                
                if line.startswith("Question:"):
                    match = re.search(r'<t>(.*?)</t>', line)
                    if match:
                        current_question = match.group(1)
                        current_options = []
                        current_exp = None
                elif line.startswith("Option"):
                    match = re.search(r'<t>(.*?)</t>', line)
                    if match:
                        current_options.append(match.group(1))
                elif line.startswith("Explanation:"):
                    match = re.search(r'<t>(.*?)</t>', line)
                    if match:
                        current_exp = match.group(1)
                elif line == "---":
                    # End of question block
                    if current_question and current_options and current_exp is not None:
                        # Reconstruct the dict format
                        trans_qs.append({
                            "q": current_question,
                            "options": current_options,
                            "exp": current_exp,
                            "correct": 0 # Placeholder, updated later
                        })
                    current_question = None
                    
            # Catch the final question if the LLM omitted the trailing "---"
            if current_question and current_options and current_exp is not None:
                trans_qs.append({
                    "q": current_question,
                    "options": current_options,
                    "exp": current_exp,
                    "correct": 0
                })
            log.dual_log(
                tag="Quiz:Translation",
                message=f"Assembled {len(trans_qs)} translated question(s).",
                payload=trans_qs,
            )

            # Re-mapping with correct indices from original
            final_translated_quizzes = []
            orig_quizzes = validated_quizzes[:QUIZ_MAX_QUESTIONS]
            
            if 'trans_qs' in locals() and len(trans_qs) == len(orig_quizzes):
                for i, t_q in enumerate(trans_qs):
                    t_q['correct'] = orig_quizzes[i]['correct']
                    t_q['q'] = t_q['q'][:250]
                    t_q['options'] = [o[:100] for o in t_q['options'][:4]]
                    t_q['exp'] = t_q['exp'][:200]
                    
                    final_id.append(f"**Q{i+1}:** {t_q['q']}")
                    final_id.append(f"  Opsi: {' | '.join(t_q['options'])}")
                    final_id.append(f"  💡 {t_q['exp']}\n")
            else:
                raise ValueError("Parsing mismatch or failure")
                
        except Exception as e:
            log.dual_log(
                tag="Quiz:Translate",
                message=f"Translation parsing failed or mismatch: {e}",
                level="WARNING",
                payload={"error": str(e)},
            )
            final_id.append("_(Translation unavailable — EN version above is authoritative.)_")
        
        # ── Complete ──
        await telemetry(self.status("Quiz generation complete.", "SUCCESS"))
        
        return "\n".join(final_en + final_id)
