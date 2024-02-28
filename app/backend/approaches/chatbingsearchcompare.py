

import os
from typing import Any, Sequence
import urllib.parse
from web_search_client import WebSearchClient
from web_search_client.models import SafeSearch
from azure.core.credentials import AzureKeyCredential
import openai
from approaches.approach import Approach
from core.messagebuilder import MessageBuilder
from core.modelhelper import get_token_limit

SUBSCRIPTION_KEY = "YourKeyHere"
ENDPOINT = "https://api.bing.microsoft.com"+  "/v7.0/"


class ChatBingSearchCompare(Approach):

    SYSTEM_MESSAGE_CHAT_CONVERSATION = """You are an Azure OpenAI Completion system. Your persona is {systemPersona} who helps answer questions. {response_length_prompt}
    User persona is {userPersona} Answer ONLY with the facts listed in the list of sources below in {query_term_language} with citations.If there isn't enough information below, say you don't know and do not give citations. For tabular information return it as an html table. Do not return markdown format.
    Your goal is to provide answers based on the facts listed below in the provided source documents. Avoid making assumptions,generating speculative or generalized information or adding personal opinions.
    
    Each source has a file name followed by a pipe character and the actual information.Use square brackets to reference the source, e.g. [url1]. Do not combine sources, list each source separately, e.g. [url1][url2].
    Never cite the source content using the examples provided in this paragraph that start with info.
      
    Here is how you should answer every question:
        
    -Look for information in the source content to answer the question in {query_term_language}.
    -If the source document has an answer, please respond with citation.You must include a citation to each document referenced only once when you find answer in source documents.      
    -If you cannot find answer in below sources, respond with I am not sure. Do not provide personal opinions or assumptions and do not include citations.
    -Identify the language of the user's question and translate the final response to that language.if the final answer is " I am not sure" then also translate it to the language of the user's question and then display translated response only. nothing else. 

    {follow_up_questions_prompt}   
    """

    COMPARATIVE_SYSTEM_MESSAGE_CHAT_CONVERSATION = """You are an Azure OpenAI Completion system. Your persona is {systemPersona} who helps compare Bing Search Response with agency data. {response_length_prompt}
    User persona is {userPersona} Answer ONLY with the facts listed in the of sources provided in {query_term_language}. If there isn't enough information, say you don't know. For tabular information return it as an html table. Do not return markdown format.
    Your goal is to provide answers based on the facts listed below in the provided Bing Search Response and Bing Search Content and compare them with Internal Documents. Avoid making assumptions, generating speculative or generalized information or adding personal opinions.
    
    You must compare what you find within the Bing Search Response with the Internal Documents response previoulsy provided in summary at the end.
      
    Here is how you should answer every question:
    -Compare information in the provided content to answer the question in {query_term_language}.      
    -If you cannot find answer in below sources, respond with I am not sure. Do not provide personal opinions or assumptions.
    -You must compare what you find within the Bing Search Response with the Internal Documents response provided.
    -If the final answer is " I am not sure" then also translate it to the {query_term_language} language and then display translated response only. nothing else.    
    
    {follow_up_questions_prompt}
    """

    QUERY_PROMPT_TEMPLATE = """Below is a history of the conversation so far, and a new question asked by the user that needs to be answered by searching in Bing Search.
    Generate a search query based on the conversation and the new question. Treat each search term as an individual keyword. Do not combine terms in quotes or brackets.
    Do not include cited sources in the search query terms.
    Do not include any text inside [] or <<<>>> in the search query terms.
    Do not include any special characters like '+'.
    If you cannot generate a search query, return just the number 0.
    """

    FOLLOW_UP_QUESTIONS_PROMPT_CONTENT = """Generate three very brief follow-up questions that the user would likely ask next about their agencies data. Use triple angle brackets to reference the questions, e.g. <<<Are there exclusions for prescriptions?>>>. Try not to repeat questions that have already been asked.
    Only generate questions and do not generate any text before or after the questions, such as 'Next Questions'
    """

    QUERY_PROMPT_FEW_SHOTS = [
        {'role' : Approach.USER, 'content' : 'What are the future plans for public transportation development?' },
        {'role' : Approach.ASSISTANT, 'content' : 'Future plans for public transportation' },
        {'role' : Approach.USER, 'content' : 'how much renewable energy was generated last year?' },
        {'role' : Approach.ASSISTANT, 'content' : 'Renewable energy generation last year' }
    ]

    RESPONSE_PROMPT_FEW_SHOTS = [
        {"role": Approach.USER ,'content': 'I am looking for information in source urls and its snippets'},
        {'role': Approach.ASSISTANT, 'content': 'user is looking for information in source urls and its snippets.'}
    ]

    COMPARATIVE_RESPONSE_PROMPT_FEW_SHOTS = [
        {"role": Approach.USER ,'content': 'I am looking for comparative information in the Bing Search Response and want to compare against the Internal Documents'},
        {'role': Approach.ASSISTANT, 'content': 'user is looking to compare information in Bing Search Response against Internal Documents.'}
    ]

    citations = {}
    approach_class = ""

    def __init__(self, model_name: str, chatgpt_deployment: str, query_term_language: str):
        self.name = "ChatBingSearchCompare"
        self.model_name = model_name
        self.chatgpt_deployment = chatgpt_deployment
        self.query_term_language = query_term_language
        self.chatgpt_token_limit = get_token_limit(model_name)
        

    async def run(self, history: Sequence[dict[str, str]], overrides: dict[str, Any]) -> Any:  

        user_query = history[-1].get("user")
        rag_answer = history[0].get("bot")
        user_persona = overrides.get("user_persona", "")
        system_persona = overrides.get("system_persona", "")
        response_length = int(overrides.get("response_length") or 1024)

        follow_up_questions_prompt = (
            self.FOLLOW_UP_QUESTIONS_PROMPT_CONTENT
            if overrides.get("suggest_followup_questions")
            else ""
        )

        # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
        messages = self.get_messages_from_history(
            self.QUERY_PROMPT_TEMPLATE,
            self.model_name,
            history,
            user_query,
            self.QUERY_PROMPT_FEW_SHOTS,
            self.chatgpt_token_limit - len(user_query)
            )
        
        query_resp = await self.make_chat_completion(messages)

        # STEP 2: Use the search query to get the top web search results
        url_snippet_dict = await self.web_search_with_answer_count_promote_and_safe_search(query_resp)
        content = ', '.join(f'{snippet} | {url}' for url, snippet in url_snippet_dict.items())

        bing_search_query = user_query + " Bing Results:\n" + content + "\n\n" #+ "Internal Documents:\n" + rag_answer + "\n\n"
        messages = self.get_messages_builder(
            self.SYSTEM_MESSAGE_CHAT_CONVERSATION.format(
                query_term_language=self.query_term_language,
                follow_up_questions_prompt=follow_up_questions_prompt,
                response_length_prompt=self.get_response_length_prompt_text(
                    response_length
                ),
                userPersona=user_persona,
                systemPersona=system_persona,
            ),
            self.model_name,
            bing_search_query,
            self.RESPONSE_PROMPT_FEW_SHOTS,
             max_tokens=4097 - 500
         )
        # STEP 3: Use the search results to answer the user's question
        bing_resp = await self.make_chat_completion(messages)


        bing_compare_query = user_query + " Bing Search Response:\n" + bing_resp + "\n\n" + "Internal Documents:\n" + rag_answer + "\n\n"

        messages = self.get_messages_builder(
            self.COMPARATIVE_SYSTEM_MESSAGE_CHAT_CONVERSATION.format(
                query_term_language=self.query_term_language,
                follow_up_questions_prompt=follow_up_questions_prompt,
                response_length_prompt=self.get_response_length_prompt_text(
                    response_length
                ),
                userPersona=user_persona,
                systemPersona=system_persona,
            ),
            self.model_name,
            bing_compare_query,
            self.COMPARATIVE_RESPONSE_PROMPT_FEW_SHOTS,
             max_tokens=4097 - 500
         )
        # Step 4: Use the search results to compare the Bing search based response with the internal documents response
        bing_compare_resp = await self.make_chat_completion(messages)

        final_response = f"{urllib.parse.unquote(bing_resp) + ' ' + urllib.parse.unquote(bing_compare_resp)}"

        return {
            "data_points": None,
            "answer": f"{urllib.parse.unquote(final_response)}",
            "thoughts": f"Searched for:<br>{user_query}<br><br>Conversations:<br>",
            "citation_lookup": self.citations
        }
    

    async def web_search_with_answer_count_promote_and_safe_search(self, user_query):
        """ WebSearchWithAnswerCountPromoteAndSafeSearch.
        """

        client = WebSearchClient(AzureKeyCredential(SUBSCRIPTION_KEY))

        try:
            web_data = client.web.search(
                query=user_query,
                answer_count=10,
                promote=["videos"],
                safe_search=SafeSearch.strict 
            )

            if web_data.web_pages.value:

                url_snippet_dict = {}
                for idx, page in enumerate(web_data.web_pages.value):
                    self.citations[f"url{idx}"] = {
                        "citation": page.url,
                        "source_path": "",
                        "page_number": "0",
                    }
                    # self.citations.append(page.url)
                    url_snippet_dict[page.url] = page.snippet.replace("[", "").replace("]", "")

                return url_snippet_dict

            else:
                print("Didn't see any Web data..")

        except Exception as err:
            print("Encountered exception. {}".format(err))

    async def make_chat_completion(self, messages):


        chat_completion = await openai.ChatCompletion.acreate(
            deployment_id=self.chatgpt_deployment,
            model=self.model_name,
            messages=messages,
            temperature=0.6,
            n=1
        )
        return chat_completion.choices[0].message.content
    
    def get_messages_builder(
        self,
        system_prompt: str,
        model_id: str,
        user_conv: str,
        few_shots = [dict[str, str]],
        max_tokens: int = 4096,
        ) -> []:
        """
        Construct a list of messages from the chat history and the user's question.
        """
        message_builder = MessageBuilder(system_prompt, model_id)

        # Few Shot prompting. Add examples to show the chat what responses we want. It will try to mimic any responses and make sure they match the rules laid out in the system message.
        for shot in few_shots:
            message_builder.append_message(shot.get('role'), shot.get('content'))

        user_content = user_conv
        append_index = len(few_shots) + 1

        message_builder.append_message(self.USER, user_content, index=append_index)

        messages = message_builder.messages
        return messages    
      


