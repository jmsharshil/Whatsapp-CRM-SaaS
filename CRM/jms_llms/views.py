from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import generics
from django.conf import settings
from openai import AzureOpenAI
from CRM.models import ChatSession, ChatMessage,Document
from CRM.jms_serializers import ChatSessionSerializer, AskSerializer
from CRM.jms_llms.retriever import get_context
import os

@ensure_csrf_cookie
def chatbot_ui(request):
    return render(request, 'chat/index.html')


_client = None
def get_client():
    global _client
    if _client is None:
        _client = AzureOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            api_version="2024-02-15-preview",
            azure_endpoint=os.environ.get("ENDPOINT_URL")
        )
    return _client


AIC_REFERENCE = (
    "\n\n---\n"
    "🚀 **Ready to turn your startup idea into reality?**\n"
    "**AIC-JNUFI** (Atal Incubation Centre – JNU Foundation for Innovation) "
    "provides world-class incubation support, expert mentorship, funding access, "
    "and state-of-the-art facilities to fuel your entrepreneurial journey.\n"
    "📧 **Contact:** aicjnufi@gmail.com  |  ✅ Apply for incubation today!"
)


class AskView(APIView):
    """POST /api/chat/ask/"""

    def post(self, request):
        serializer = AskSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        question   = serializer.validated_data["question"]
        session_id = serializer.validated_data.get("session_id")

        # Get or create session
        if session_id:
            try:
                session = ChatSession.objects.get(pk=session_id)
            except ChatSession.DoesNotExist:
                return Response({"error": "Session not found."}, status=404)
        else:
            title   = question[:60] + ("…" if len(question) > 60 else "")
            session = ChatSession.objects.create(title=title)

        # Build KB context (cached)
        from django.core.cache import cache
        doc_list = cache.get("kb_doc_list")
        if not doc_list:
            all_docs = Document.objects.all().values("id", "name", "extracted_text")
            doc_list = [
                {"doc_id": d["id"], "name": d["name"], "content": d["extracted_text"]}
                for d in all_docs
            ]
            cache.set("kb_doc_list", doc_list, timeout=300)

        context = get_context(question, doc_list)
        has_kb  = bool(context)
        kb_section = ("ADDITIONAL CONTEXT FROM KNOWLEDGE BASE:\n" + context) if has_kb else ""

        # Single unified system prompt
        system_prompt = f"""You are Naavy, a seasoned startup mentor combining the expertise of a serial entrepreneur CEO (20+ years) and a deep-knowledge Incubation Advisor. You are a NEUTRAL, independent advisor — you do NOT represent any specific organisation or incubator.

YOUR ROLE:
- Give warm, honest, practical, and highly motivating advice tailored to the founder's specific situation.
- Help with: startup ideation, idea validation, business model design, MVP strategy, team building, fundraising, investor pitch preparation, growth hacking, and founder mindset/resilience.
- Explain incubation programs, equity-free grants, government schemes (Startup India, DPIIT recognition, Atal Innovation Mission, BIRAC, DST, etc.), and accelerator models clearly.
- Guide founders on crafting a compelling incubation application: story, problem statement, market size, traction, team.
- Celebrate the founder's courage to start. Many will be first-timers — inspire them.
- Be direct but empathetic. Ask clarifying questions when you need more context.
- Reference proven frameworks where relevant: Lean Startup, Jobs-to-be-Done, First Principles Thinking, OKRs, Blue Ocean Strategy, etc.
- For existing startups: diagnose problems clearly, offer turnaround strategies, and push for bold thinking.
- Demystify startup jargon. Be clear, accessible, and encouraging.

{kb_section}
RULES:
1. If KB context is relevant to the question, use it to enrich your answer and end with [SOURCE:KB].
2. Otherwise use your deep expertise and end with [SOURCE:LLM].
3. Always close with a concrete, specific **⚡ Next Step** the founder can act on TODAY.
4. After the Next Step, always append this exact AIC-JNUFI reference block verbatim:
5. Before the Next Step, add a **🏆 Real-World Inspiration** section — list 2-3 real company names (Indian startups preferred) that succeeded in a similar space. One line each: company name + what problem they solved.
6. After Real-World Inspiration, add a **🤝 How AIC-JNUFI Can Help You** section — using the knowledge base context, write 2-3 specific sentences on what AIC-JNUFI offers relevant to this founder's situation.
{AIC_REFERENCE}"""

        # Last 6 messages as history
        history = [
            {"role": m.role, "content": m.content}
            for m in session.messages.order_by("-created_at")[:6][::-1]
        ]

        messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": question}]
        )

        # Call Azure OpenAI
        try:
            response = get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=1200,
                temperature=0.5,
            )
            answer = response.choices[0].message.content.strip()
        except Exception as e:
            return Response({"error": str(e)}, status=502)

        # Detect source tag
        source = "kb" if "[SOURCE:KB]" in answer else "llm"
        answer = answer.replace("[SOURCE:KB]", "").replace("[SOURCE:LLM]", "").strip()
        
        # Remove model metadata lines
        answer = answer.replace("You are trained on data up to October 2023.", "").strip()
        answer = "\n".join(line for line in answer.split("\n") if "trained on data up to" not in line.lower()).strip()

        # Save to DB
        ChatMessage.objects.create(session=session, role="user",      content=question)
        ChatMessage.objects.create(session=session, role="assistant", content=answer, source=source)

        return Response({
            "session_id":   session.id,
            "question":     question,
            "answer":       answer,
            "source":       source,
        })

from django.http import StreamingHttpResponse
import json
import time

class AskStreamView(APIView):
    """POST /api/chat/ask/stream/"""
    
    # Questionnaire questions
    QUESTIONS = [
        {
            "id": "name",
            "question": "👤 **What's your startup name?**\n",
            "field": "founder_name"
        },
        {
            "id": "idea",
            "question": "💡 **Describe your startup idea in a few lines.**\n",
            "field": "startup_idea"
        },
        {
            "id": "stage",
            "question": "🚀 **What stage is your startup at?**\n\n• Ideation (just an idea)\n• MVP Development (building MVP)\n• Early Traction (launched, getting users)\n• Scaling (product-market fit, scaling)\n• Incubated (in incubation program)\n• Established (mature business)",
            "field": "startup_stage"
        },
        {
            "id": "location",
            "question": "📍 **Where are you located?**\n",
            "field": "location"
        },
        {
            "id": "funding",
            "question": "💰 **What's your current funding stage?**\n\n• No Funding Yet\n• Bootstrapped/Self-Funded\n• Seeking Angel Investment\n• Seed Funded\n• Series A+",
            "field": "funding_stage"
        }
    ]

    def get_current_question_index(self, session):
        """Get which question we're on based on filled fields"""
        # Check which profile fields are filled
        for i, q in enumerate(self.QUESTIONS):
            field = q["field"]
            if not getattr(session, field, None):
                return i
        return -1  # All questions answered

    def parse_stage_answer(self, text):
        """Parse stage answer from user"""
        text = text.lower().strip()
        if any(w in text for w in ['ideation', 'idea', 'just', 'concept']):
            return 'ideation'
        elif any(w in text for w in ['mvp', 'building', 'develop']):
            return 'mvp'
        elif any(w in text for w in ['traction', 'launched', 'users', 'early']):
            return 'traction'
        elif any(w in text for w in ['scaling', 'scale', 'growth']):
            return 'scaling'
        elif any(w in text for w in ['incubated', 'incubation', 'incubator']):
            return 'incubated'
        elif any(w in text for w in ['established', 'mature', 'revenue']):
            return 'established'
        return None

    def parse_funding_answer(self, text):
        """Parse funding answer from user"""
        text = text.lower().strip()
        if any(w in text for w in ['no', 'none', 'funding yet', "haven't"]):
            return 'no_funding'
        elif any(w in text for w in ['bootstrap', 'self', 'own']):
            return 'bootstrapped'
        elif any(w in text for w in ['angel', 'seeking']):
            return 'seeking_angel'
        elif any(w in text for w in ['seed', 'funded']):
            return 'seed_funded'
        elif any(w in text for w in ['series', 'round']):
            return 'series_a'
        return None

    def post(self, request):
        t_request_start = time.time()
        print(f"\n[TIMING] === REQUEST START ===")
        
        serializer = AskSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        question   = serializer.validated_data["question"]
        session_id = serializer.validated_data.get("session_id")

        # Get or create session
        is_new_session = False
        if session_id:
            try:
                session = ChatSession.objects.get(pk=session_id)
            except ChatSession.DoesNotExist:
                return Response({"error": "Session not found."}, status=404)
        else:
            is_new_session = True
            session = ChatSession.objects.create(title="New Chat")

        def stream_generator():
            nonlocal session
            
            # Yield session ID first
            yield f"data: {json.dumps({'type': 'session', 'session_id': session.id})}\n\n"

            # Check if this is a brand new session (show welcome message)
            if session.messages.count() == 0 and not session.questionnaire_completed:
                # Welcome message on first interaction
                welcome_msg = """🚀 **Welcome to your AI Startup Mentor!**

I'm Naavya here to guide you through your entrepreneurial journey with personalized mentorship, expert advice, and real-world insights tailored to your startup stage.
**Go ahead—ask me anything about your startup!**  💡"""

                ChatMessage.objects.create(session=session, role="assistant", content=welcome_msg, source="")
                
                yield f"data: {json.dumps({'type': 'token', 'token': welcome_msg})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'source': ''})}\n\n"
                
                total_elapsed = time.time() - t_request_start
                print(f"[TIMING] === REQUEST COMPLETE (Welcome + Name Q) in {total_elapsed:.3f}s ===\n")
                return

            # Check if questionnaire is completed
            current_q_idx = self.get_current_question_index(session)
            
            # If questionnaire NOT completed and we have empty fields, collect info
            # This starts after welcome message when user sends first message
            if current_q_idx >= 0 and not session.questionnaire_completed:
                # Questionnaire mode: collect info
                
                # SAFETY: Always ensure founder_name is asked FIRST if empty
                if not session.founder_name:
                    current_q_idx = 0
                
                current_q = self.QUESTIONS[current_q_idx]
                q_id = current_q["id"]
                field_name = current_q["field"]

                # Save user message
                if question.strip():
                    ChatMessage.objects.create(session=session, role="user", content=question)

                # Only save answer if bot already asked THIS question
                last_bot_msg = session.messages.filter(role="assistant").order_by("-created_at").first()
                bot_already_asked = last_bot_msg and current_q["question"].strip()[:30] in last_bot_msg.content

                if question.strip() and bot_already_asked:
                    setattr(session, field_name, question.strip())
                    session.save()

                # Determine next question or completion
                next_q_idx = self.get_current_question_index(session)
                
                if next_q_idx == -1:
                    # All required questions answered!
                    session.questionnaire_completed = True
                    session.save()
                    
                    # Build personalized intro message
                    intro = f"""Perfect! Thank you! 🎉

I now have a good understanding of your startup:
- **Idea:** {session.startup_idea}
- **Stage:** {session.get_startup_stage_display()}
- **Location:** {session.location}
- **Funding:** {session.get_funding_stage_display()}

I'm ready to be your trusted startup mentor. You can now ask me anything about:
✅ Business model validation
✅ MVP strategy and growth
✅ Fundraising & investor pitch
✅ Team building
✅ Incubation program guidance
✅ Startup India & government schemes
✅ Founder mindset & resilience

**Go ahead—ask me your question!** 🚀"""

                    response_msg = intro
                    ChatMessage.objects.create(session=session, role="assistant", content=response_msg, source="")
                    
                    yield f"data: {json.dumps({'type': 'token', 'token': response_msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'source': '', 'questionnaire_completed': True})}\n\n"
                else:
                    # Ask next question
                    next_q = self.QUESTIONS[next_q_idx]
                    response_msg = next_q["question"]
                    ChatMessage.objects.create(session=session, role="assistant", content=response_msg, source="")
                    
                    yield f"data: {json.dumps({'type': 'token', 'token': response_msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'source': ''})}\n\n"

                total_elapsed = time.time() - t_request_start
                print(f"[TIMING] === REQUEST COMPLETE (Questionnaire) in {total_elapsed:.3f}s ===\n")
                return

            # ========== NORMAL CHAT MODE (questionnaire_completed) ==========
            # Save user message
            ChatMessage.objects.create(session=session, role="user", content=question)

            # Build KB context (cached)
            from django.core.cache import cache
            t1 = time.time()
            doc_list = cache.get("kb_doc_list")
            if not doc_list:
                all_docs = Document.objects.all().values("id", "name", "extracted_text")
                doc_list = [
                    {"doc_id": d["id"], "name": d["name"], "content": d["extracted_text"]}
                    for d in all_docs
                ]
                cache.set("kb_doc_list", doc_list, timeout=300)
                print(f"[TIMING] Fetched {len(doc_list)} documents from DB: {time.time()-t1:.3f}s")
            else:
                print(f"[TIMING] Used {len(doc_list)} documents from cache")

            context = get_context(question, doc_list)
            has_kb  = bool(context)

            # Stage-specific guidance
            stage_guidance = {
                'ideation': """STAGE-SPECIFIC FOCUS FOR IDEATION:
- Help validate the business idea: Is there a real problem? Is there a market?
- Guide on idea validation techniques: customer interviews, lean canvas, problem-solution fit
- Discuss MVPs: what's the minimum viable product to test this idea?
- Address founder concerns: imposter syndrome, idea theft fears, competition
- Point out common pitfalls: building in stealth, ignoring customer feedback""",
                
                'mvp': """STAGE-SPECIFIC FOCUS FOR MVP DEVELOPMENT:
- Guide on building fast and cheap: MVP principles, no-code tools, lean development
- Help prioritize MVP features: focus on core value proposition only
- Discuss GTM (go-to-market): how to get first customers/users
- Timeline expectations: how long to build MVP, when to pivot vs persevere
- Tech stack considerations: scalability vs speed, technical debt""",
                
                'traction': """STAGE-SPECIFIC FOCUS FOR EARLY TRACTION:
- Scaling strategies: How to grow users/revenue systematically
- Customer retention: reducing churn, improving LTV (lifetime value)
- Unit economics: understanding CAC (customer acquisition cost), payback period
- Fundraising readiness: what metrics investors want to see?
- Team building: when and how to hire first employees""",
                
                'scaling': """STAGE-SPECIFIC FOCUS FOR SCALING:
- Growth acceleration: marketing strategies, partnerships, distribution channels
- Funding rounds: Series A preparation, pitch deck, investor relations
- Organizational scaling: building high-performing teams, culture, processes
- Market expansion: geographic expansion, new product lines, adjacent markets
- Profitability vs growth: unit economics at scale, path to profitability""",
                
                'incubated': """STAGE-SPECIFIC FOCUS FOR INCUBATED STARTUPS:
- Maximize incubation program value: leverage mentors, investors, resources
- Traction while incubated: set aggressive growth targets during program
- Post-incubation planning: fundraising, market entry strategy
- Investor connections: build relationships while in program, prepare for pitch
- Graduation readiness: milestones to achieve before program ends""",
                
                'established': """STAGE-SPECIFIC FOCUS FOR ESTABLISHED BUSINESSES:
- Sustainability and optimization: improving margins, operational efficiency
- New revenue streams: product lines, adjacent markets, licensing
- Exit strategy or long-term vision: acquisition, IPO, or organic growth?
- Innovation and staying competitive: R&D, disruption, market trends
- Scaling profitably: balancing growth with profitability"""
            }

            # Get stage-specific guidance
            stage_key = session.startup_stage
            stage_guide = stage_guidance.get(stage_key, "")

            # Personalized system prompt with founder's info
            founder_context = f"""
Founder Profile:
- Name: {session.founder_name}
- Startup Idea: {session.startup_idea}
- Current Stage: {session.get_startup_stage_display()}
- Location: {session.location}
- Funding Stage: {session.get_funding_stage_display()}

{stage_guide}
"""
            kb_section = ("ADDITIONAL CONTEXT FROM KNOWLEDGE BASE:\n" + context) if has_kb else ""

            system_prompt = f"""You are a seasoned startup mentor combining the expertise of a serial entrepreneur CEO (20+ years) and a deep-knowledge Incubation Advisor. You are a NEUTRAL, independent advisor — you do NOT represent any specific organisation or incubator.

{founder_context}


YOUR ROLE:
- Give warm, honest, practical, and highly motivating advice tailored to the founder's specific situation.
- Help with: startup ideation, idea validation, business model design, MVP strategy, team building, fundraising, investor pitch preparation, growth hacking, and founder mindset/resilience.
- Explain incubation programs, equity-free grants, government schemes (Startup India, DPIIT recognition, Atal Innovation Mission, BIRAC, DST, etc.), and accelerator models clearly.
- Guide founders on crafting a compelling incubation application: story, problem statement, market size, traction, team.
- Celebrate the founder's courage to start. Many will be first-timers — inspire them.
- Be direct but empathetic. Ask clarifying questions when you need more context.
- Reference proven frameworks where relevant: Lean Startup, Jobs-to-be-Done, First Principles Thinking, OKRs, Blue Ocean Strategy, etc.
- For existing startups: diagnose problems clearly, offer turnaround strategies, and push for bold thinking.
- Demystify startup jargon. Be clear, accessible, and encouraging.
{kb_section}
 
RULES:
1. If KB context is relevant to the question, use it to enrich your answer and end with [SOURCE:KB].
2. Otherwise use your deep expertise and end with [SOURCE:LLM].
3. Always close with a concrete, specific **⚡ Next Step** the founder can act on TODAY.
4. Before the Next Step, add a **🏆 Real-World Inspiration** section — list 2-3 real company names (Indian startups preferred) that succeeded in a similar space. One line each: company name + what problem they solved.
5. After Real-World Inspiration, add a **🤝 How AIC-JNUFI Can Help You** section — using the knowledge base context, write 2-3 specific sentences on what AIC-JNUFI offers relevant to this founder's situation.
6. ALWAYS end your response with the following reference block (copy it verbatim):

6. ALWAYS end your response with the following reference block (copy it verbatim):

{AIC_REFERENCE}"""

            history = [
                {"role": m.role, "content": m.content}
                for m in session.messages.only("role", "content").exclude(role="assistant", source="system").order_by("-created_at")[:6][::-1]
            ]

            messages = (
                [{"role": "system", "content": system_prompt}]
                + history
                + [{"role": "user", "content": question}]
            )

            try:
                t3 = time.time()
                print(f"[TIMING] Calling Azure API...")
                response = get_client().chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=1200,
                    temperature=0.5,
                    stream=True,
                )

                full_answer = ""
                first_token_time = None
                for chunk in response:
                    if first_token_time is None:
                        first_token_time = time.time()
                        api_time = first_token_time - t3
                        print(f"[TIMING] First token from Azure: {api_time:.3f}s")
                    
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        full_answer += token
                        yield f"data: {json.dumps({'type': 'token', 'token': token})}\n\n"
                
                t4 = time.time()
                api_total = t4-t3
                print(f"[TIMING] Azure API total: {api_total:.3f}s")

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                return

            # Detect source
            source = "kb" if "[SOURCE:KB]" in full_answer else "llm"
            full_answer = full_answer.replace("[SOURCE:KB]", "").replace("[SOURCE:LLM]", "").strip()
            
            # Remove model metadata lines
            full_answer = full_answer.replace("You are trained on data up to October 2023.", "").strip()
            full_answer = "\n".join(line for line in full_answer.split("\n") if "trained on data up to" not in line.lower()).strip()

            # Save assistant message
            ChatMessage.objects.create(
                session=session, role="assistant",
                content=full_answer, source=source
            )

            # Final chunk: metadata
            yield f"data: {json.dumps({'type': 'done', 'source': source})}\n\n"
            
            total_elapsed = time.time() - t_request_start
            print(f"[TIMING] === REQUEST COMPLETE in {total_elapsed:.3f}s ===\n")

        return StreamingHttpResponse(
            stream_generator(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disables nginx buffering
            }
        )


class SessionListView(generics.ListAPIView):
    """GET /api/chat/sessions/"""
    queryset         = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer


class SessionDetailView(generics.RetrieveAPIView):
    """GET /api/chat/sessions/<id>/"""
    queryset         = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer


class SessionDeleteView(generics.DestroyAPIView):
    """DELETE /api/chat/sessions/<id>/delete/"""
    queryset = ChatSession.objects.all()