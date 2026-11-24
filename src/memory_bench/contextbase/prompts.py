"""Pinned historical prompts for #115 and #119; evaluation-only gold inputs stay in judge functions.

LongMemEval author: xiaowu0162/LongMemEval, evaluate_qa.py, 9e0b455f.
AMB: vectorize-io/agent-memory-benchmark, bcc9dcd6 (BEAM), 28b644bd (LME).
"""

import json


def lme_reader(
    query: str,
    context: str,
    task_type: str,
    split: str,
    category: str | None = None,
    meta: dict | None = None,
) -> str:
    meta = meta or {}
    raw = meta.get("_raw_response")
    ctx = json.dumps(raw) if raw else context
    question_date = meta.get("query_timestamp")
    formatted_date = question_date if question_date else "Not specified"
    return f"""You are a helpful assistant that must answer user questions based on the previous conversations.\n\n**Answer Guidelines:**\n1. Start by scanning retrieved context to understand the facts and events that happened and the timeline.\n2. Reason about all the memories and find the right answer, considering the most recent memory as an update of the current facts.\n3. If you have 2 possible answers, just say both.\n\nIn general the answer must be comprehensive and plenty of details from the retrieved context.\n\nFor quantitative/counting questions ("how many..."): First list each unique item in your reasoning (1. X, 2. Y, 3. Z...), scanning ALL facts, then count them for your answer.\nIf questions asks a location (where...?) make sure to include the location name.\nFor recommendation questions ("can you recommend...", "suggest...", "any tips..."): DO NOT give actual recommendations. Instead, describe what KIND the user would prefer based on their context. Example answer format: "The user would prefer recommendations for [category] that focus on [their interest]. They would not prefer [what to avoid based on context]."\nFor questions asking for help or instructions, consider the users' recent memories and previous interactions with the assistant to understand their current situation better (recent purchases, specific product models used..)\nFor specific number/value questions, use the context to understand what is the most up-to-date number based on recency, but also include the reasoning (in the answer) on previous possible values and why you think are less relevant.\nFor open questions, include as much details as possible from different sources that are relevant.\nFor questions where a specific entity/role is mentioned and it's different from your memory, just say the truth, don't make up anything just to fulfill the question. For example, if the question is about a specific sport, you should consider if the memories and the question are about the same sport. (e.g. american football vs soccer, shows vs podcasts)\nFor comparative questions, say you don't know the answer if you don't have information about both sides. (or more sides)\nFor questions related to time/date, carefully review the question date and the memories date to correctly answer the question.\nFor questions related to time/date calculation (e.g. How many days passed between X and Y?), carefully review the memories date to correctly answer the question and only provide an answer if you have information about both X and Y, otherwise say it's not possible to calculate and why.\n\nConsider assistant's previous actions (e.g., bookings, reminders) as impactful to the user experiences.\n\n\nQuestion: {query}\nQuestion Date: {formatted_date}\n\nRetrieved Context:\n{ctx}\n\n\nAnswer:\n"""


def beam_reader(
    query: str,
    context: str,
    task_type: str,
    split: str,
    category: str | None = None,
    meta: dict | None = None,
) -> str:
    return f"""You are an AI assistant with access to stored memories from prior conversations with a user.\nUse these memories to answer the following question as accurately and completely as possible.\n\nIMPORTANT RULES:\n1. Scan ALL provided memories before answering — do not stop after the first relevant one.\n2. If multiple memories contain relevant information, combine and cross-reference them.\n3. The question may refer to something indirectly (e.g. 'the person I met at the festival') — resolve such references by searching the memories before concluding. Only if the specific information is genuinely absent after a careful search, say exactly: "I don't have enough information to answer this question."\n4. Be specific and direct — include exact names, dates, numbers, and details from the memories.\n5. Do NOT invent or assume information that isn't in the memories.\n\nQUESTION: {query}\n\nRETRIEVED MEMORIES:\n{context}\n\nANSWER:"""


def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ["single-session-user", "single-session-assistant", "multi-session"]:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == "temporal-reasoning":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == "knowledge-update":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == "single-session-preference":
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response)
    return prompt


def beam_judge(query, answer, rubric_item):
    prompt = f'Evaluate whether the following LLM response demonstrates compliance with the specified RUBRIC CRITERION.\n\nQUESTION:\n{query}\n\nLLM RESPONSE:\n{answer}\n\nRUBRIC CRITERION:\n{rubric_item}\n\nSCORING GUIDELINES:\n\nFirst, determine whether the rubric criterion is a POSITIVE requirement (the response SHOULD include something) or a NEGATIVE constraint (the response SHOULD NOT include something).\n\n**For POSITIVE requirements** (response should contain, mention, or demonstrate something):\n- **1.0 (Complete Compliance)**: The required element is present, accurate, and complete.\n- **0.5 (Partial Compliance)**: The required element is partially present, has minor inaccuracies, or is incomplete.\n- **0.0 (No Compliance)**: The required element is missing, incorrect, or the response is entirely off-topic / non-responsive.\n\n**For NEGATIVE constraints** (response should NOT contain or should avoid something):\n- **1.0**: The response is responsive AND the prohibited element is absent.\n- **0.5**: The response is responsive but contains a borderline or ambiguous reference to the prohibited element.\n- **0.0**: The prohibited element is present, OR the response is non-responsive.\n\n**Compound statements** ("and"/commas joining multiple required elements): all present=1.0, some=0.5, none=0.0.\n\nEVALUATION RULES:\n1. Semantic tolerance: paraphrases and synonyms are acceptable.\n2. Numeric/date equivalence: "$68,000"="68k"="sixty-eight thousand dollars"; "2 years"="24 months".\n3. Case/punctuation/whitespace tolerance.\n4. Hedging tolerance: do not penalize "I think"/"probably"/passive/verbosity if content satisfies the criterion.\n5. Style neutrality: do not penalize tone/format/length unless the criterion requires it.\n6. Responsiveness: completely off-topic or refusal = 0.0.\n7. Independence: evaluate this criterion in isolation.\n8. Specificity: vague generic answers score lower than specific, detailed ones.\n\nReturn JSON: {{"score": <0.0 or 0.5 or 1.0>, "reason": "<one concise sentence>"}}'
    return prompt
