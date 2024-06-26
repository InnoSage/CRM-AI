from fastapi import FastAPI, HTTPException, Query
from typing import Optional
import mysql.connector
from mysql.connector import Error
import pandas as pd
import os
import shutil
from openai import OpenAI
import config
from groq import Groq
import duckdb
import sqlparse
import json
from model.QueryRequest import QueryRequest
from model.ResponseModel import SuccessResponseModel, ErrorResponseModel

import deepl
import requests
from pydantic import BaseModel

app = FastAPI()


SYSTEM_TEMPLATE = """Please read the columns of the dataframe and provide a description of their attributes. write it in the same form as the example.
###Example
[DateFrame]
{}

[Attributes]
회사명 (VARCHAR): The name of the company.
카테고리 (VARCHAR): The industry category to which the company belongs (e.g., 소프트웨어, 컨설팅, 제조업).
거래단계 (VARCHAR): The current stage of the transaction with the company (e.g., 잠재고객, 협상 중, 자격 확인됨).
회사설명 (TEXT): A brief description of the company's business and specialization.
도메인 (VARCHAR): The company's website domain.
투자단계 (VARCHAR): The investment stage of the company (e.g., 시리즈 A, 시리즈 B, 시리즈 C).
거래일 (DATE): The date of the transaction.
거래금액 (INTEGER): The amount of money involved in the transaction."""

USER_TEMPLATE = """[DateFrame]
{}

[Attributes]"""

NEW_PROMPT_TEMPLATE = """You are Groq Advisor, and you are tasked with generating SQL queries for DuckDB based on user questions about data stored in table:
[Table] 
table_{file_name}

[Columns]
{atttributes}


Given a user's question about this data, write a valid DuckDB SQL query that accurately extracts or calculates the requested information from these tables and adheres to SQL best practices for DuckDB, optimizing for readability and performance where applicable.

Here are some tips for writing DuckDB queries:
* DuckDB syntax requires querying from the table. For example: SELECT * FROM table_{organization_id}_{sheet_id}
* All tables referenced MUST be aliased
* DuckDB does not implicitly include a GROUP BY clause
* CURRENT_DATE gets today's date
* Aggregated fields like COUNT(*) must be appropriately named

Question:
--------
{{user_question}}
--------
Reminder: Generate a DuckDB SQL to answer to the question:
* respond as a valid JSON Document
* [Best] If the question can be answered with the available tables: {{{{"sql": <sql here>}}}}
* If the question cannot be answered with the available tables: {{{{"error": <explanation here>}}}}
* Ensure that the entire output is returned on only one single line
* Keep your query as simple and straightforward as possible; do not use subqueries"""


def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
        )
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error: {e}")
        return None


def generate_prompt_and_update_files(
    update_csv_path, save_csv_path, organization_id, sheet_id
):
    client = OpenAI(api_key=config.GPT_SERVER_KEY)

    if os.path.exists(save_csv_path):
        # CSV Updating Process
        OLD = pd.read_csv(save_csv_path)
        OLD_MD = OLD[:10].to_markdown(index=False)
        old_attribute = list(OLD.columns)

        NEW = pd.read_csv(update_csv_path)
        NEW_MD = NEW.to_markdown(index=False)
        new_attribute = list(NEW.columns)
        FILE_NAME = update_csv_path.split("/")[-1].split(".")[0]

        # Prompt Updating
        if old_attribute != new_attribute:
            system_prompt = SYSTEM_TEMPLATE.format(OLD_MD)
            user_prompt = USER_TEMPLATE.format(NEW_MD)

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
            )

            output = response.choices[0].message.content.replace("[Attributes]\n", "")
            new_prompt = NEW_PROMPT_TEMPLATE.format(
                file_name=FILE_NAME,
                atttributes=output,
                organization_id=organization_id,
                sheet_id=sheet_id,
            )

            prompt_path = "./prompts/{}_prompt.txt".format(FILE_NAME)
            with open(prompt_path, "r", encoding="utf-8") as file:
                old_prompt = file.readlines()
                old_prompt = "".join(old_prompt)
            with open(prompt_path, "w", encoding="utf-8") as file:
                file.writelines(new_prompt)

        # CSV Updating
        shutil.move(update_csv_path, save_csv_path)

    else:
        # CSV ADD Process
        DEFAULT_DF = pd.read_csv("./data/default.csv")

        NEW = pd.read_csv(update_csv_path)
        NEW_MD = NEW.to_markdown(index=False)
        FILE_NAME = update_csv_path.split("/")[-1].split(".")[0]

        system_prompt = SYSTEM_TEMPLATE.format(DEFAULT_DF)
        user_prompt = USER_TEMPLATE.format(NEW_MD)

        # Prompt ADD
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )

        output = response.choices[0].message.content.replace("[Attributes]\n", "")
        new_prompt = NEW_PROMPT_TEMPLATE.format(
            file_name=FILE_NAME,
            atttributes=output,
            organization_id=organization_id,
            sheet_id=sheet_id,
        )

        prompt_path = "./prompts/{}_prompt.txt".format(FILE_NAME.split(".")[0])
        with open(prompt_path, "w", encoding="utf-8") as file:
            file.write(new_prompt)

        save_csv_path = update_csv_path.replace("data/tmp/", "data/")

        # CSV ADD
        shutil.move(update_csv_path, save_csv_path)


@app.get("/update-sheet/{organization_id}/{sheet_id}")
def export_csv(organization_id: int, sheet_id: int):
    connection = get_db_connection()
    if connection is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = connection.cursor(dictionary=True)

        # Fetch attribute names for CSV header
        cursor.execute(
            """
        SELECT a.id AS attribute_id, a.name AS attribute_name
        FROM attribute a
        JOIN sheet s ON a.sheet_id = s.id
        JOIN organization o ON s.organization_id = o.organization_id
        WHERE o.organization_id = %s
          AND s.id = %s;
        """,
            (organization_id, sheet_id),
        )
        attributes = cursor.fetchall()

        if not attributes:
            raise HTTPException(
                status_code=404,
                detail="No attributes found for the given organization and sheet",
            )

        attribute_ids = [attribute["attribute_id"] for attribute in attributes]
        headers = ["company_name"] + [
            attribute["attribute_name"] for attribute in attributes
        ]

        # Create dynamic query part for attributes
        attribute_cases = ", ".join(
            [
                f"MAX(CASE WHEN attribute_id = {attr['attribute_id']} THEN value END) AS {attr['attribute_name']}"
                for attr in attributes
            ]
        )

        # Fetch content values for the attributes
        query = f"""
        SELECT c.name AS company_name, {attribute_cases}
        FROM content ct
        JOIN deal d ON ct.deal_id = d.id
        JOIN company c ON d.company_id = c.id
        WHERE attribute_id IN ({', '.join(map(str, attribute_ids))})
        GROUP BY c.name
        """
        cursor.execute(query)
        content_values = cursor.fetchall()

        # Create DataFrame and save to CSV
        df = pd.DataFrame(content_values, columns=headers)
        if not os.path.exists("data/tmp"):
            os.makedirs("data/tmp")
        update_csv_path = f"data/tmp/{organization_id}_{sheet_id}.csv"
        save_csv_path = f"data/{organization_id}_{sheet_id}.csv"
        df.to_csv(update_csv_path, index=False)

        # Generate prompt and update files
        generate_prompt_and_update_files(
            update_csv_path, save_csv_path, organization_id, sheet_id
        )

        return SuccessResponseModel(data={"file_path": save_csv_path})

    except Error as e:
        return ErrorResponseModel(code="DB_ERROR", message=f"Database error: {e}")

    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()


def chat_with_groq(client, incontext, prompt, model):
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": incontext},
            {"role": "user", "content": prompt},
        ],
        temperature=0.8,
        top_p=0.9,
    )

    return completion.choices[0].message.content


def execute_duckdb_query(query, organization_id, sheet_id):
    original_cwd = os.getcwd()
    os.chdir("data")

    file_name = f"{organization_id}_{sheet_id}.csv"
    print(f"Executing query: {query}")
    print("file_name:", file_name)

    try:
        conn = duckdb.connect(database=":memory:", read_only=False)
        table_name = f"table_{organization_id}_{sheet_id}"
        # 파일이 존재하는지 확인하고 테이블로 로드
        if os.path.exists(file_name):
            conn.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{file_name}')"
            )
            # print(f"Table {table_name} created and data loaded.")
            # 필요한 작업 수행 (예시로 테이블 조회)
            query_result = conn.execute(f"SELECT * FROM {table_name}").fetchdf()
            print(query_result)
            query_result = conn.execute(query).fetchdf().reset_index(drop=True)

        else:
            print(f"File {file_name} does not exist.")
    finally:
        # 작업이 끝나면 테이블 삭제
        if os.path.exists(file_name):
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            print(f"Table {table_name} deleted.")

        # DuckDB 연결 종료
        conn.close()

        # 작업 디렉토리를 원래의 디렉토리로 복원
        os.chdir(original_cwd)

    return query_result


def get_json_output(llm_response):
    llm_response_no_escape = (
        llm_response.replace("\\n", " ").replace("\n", " ").replace("\\", "").strip()
    )
    open_idx = llm_response_no_escape.find("{")
    close_idx = llm_response_no_escape.rindex("}") + 1
    cleaned_result = llm_response_no_escape[open_idx:close_idx]
    json_result = json.loads(cleaned_result)
    if "sql" in json_result:
        query = json_result["sql"]
        return True, sqlparse.format(query, reindent=True, keyword_case="upper")
    elif "error" in json_result:
        return False, json_result["error"]


def get_reflection(client, incontext, full_prompt, llm_response, model):
    reflection_prompt = f"""
    You were giving the following prompt:

    {full_prompt}

    This was your response:

    {llm_response}

    There was an error with the response, either in the output format or the query itself.

    Ensure that the following rules are satisfied when correcting your response:
    1. SQL is valid DuckDB SQL, given the provided metadata and the DuckDB querying rules
    2. The query SPECIFICALLY references the correct tables: table_default, and those tables are properly aliased? (this is the most likely cause of failure)
    3. Response is in the correct format ({{sql: <sql_here>}} or {{"error": <explanation here>}}) with no additional text?
    4. All fields are appropriately named
    5. There are no unnecessary sub-queries
    6. ALL TABLES are aliased (extremely important)

    Rewrite the response and respond ONLY with the valid output format with no additional commentary
    """
    return chat_with_groq(client, incontext, reflection_prompt, model)


def get_summarization(client, user_question, df, model):
    prompt = f"""
    A user asked the following question pertaining to local database tables:

    {user_question}

    To answer the question, a dataframe was returned:

    Dataframe:
    {df}

    In a few sentences, summarize the data in the table as it pertains to the original user question. Avoid qualifiers like "based on the data" and do not comment on the structure or metadata of the table itself
    """.format(
        user_question=user_question, df=df
    )
    
    return chat_with_groq(client, "", prompt, model)


@app.post("/generate-query")
def query_data(request: QueryRequest):
    try:
        # Get the Groq API key and create a Groq client
        organization_id = request.organization_id
        sheet_id = request.sheet_id

        # 모델 선택
        api_key = config.GROQ_API_KEY
        url = "https://api.groq.com/openai/v1/models"

        # API 요청을 위한 헤더를 설정합니다.
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # API 요청을 보내고 응답을 받습니다.
        response = requests.get(url, headers=headers)
        models = response.json()

        # 모델 리스트
        origin_model = [
            "llama3-70b-8192",
            "mixtral-8x7b-32768",
            "llama3-8b-8192",
            "gemma-7b-it",
        ]

        # 활성화된 모델만 포함하는 리스트를 생성합니다.
        active_models = [
            model
            for model in origin_model
            if any(m["id"] == model and m["active"] for m in models["data"])
        ]

        model = active_models[0]

        print("Selected model:", model)
        max_num_reflections = 5

        client = Groq(api_key=config.GROQ_API_KEY)

        with open(f"prompts/base_prompt.txt", "r") as file:
            base_prompt = file.read()

        # Load the base prompt
        with open(f"prompts/{organization_id}_{sheet_id}_prompt.txt", "r") as file:
            input_prompt = file.read()

        # Generate the full prompt for the AI
        full_prompt = input_prompt.format(user_question=request.user_question)

        # Get the AI's response
        llm_response = chat_with_groq(client, base_prompt, full_prompt, model)
        is_sql, result = get_json_output(llm_response)
        # Try to process the AI's response
        valid_response = False
        i = 0
        while valid_response is False and i < max_num_reflections:
            try:
                # Check if the AI's response contains a SQL query or an error message
                is_sql, result = get_json_output(llm_response)
                if is_sql:
                    # If the response contains a SQL query, execute it
                    results_df = execute_duckdb_query(result, organization_id, sheet_id)
                    valid_response = True
                else:
                    # If the response contains an error message, it's considered valid
                    valid_response = True
            except:
                # If there was an error processing the AI's response, get a reflection
                llm_response = get_reflection(
                    client, base_prompt, full_prompt, llm_response, model
                )
                i += 1

        # Prepare the result to be returned
        if is_sql:
            # If the result is a SQL query, display the query and the resulting data

            summarization = get_summarization(
                client,
                request.user_question,
                results_df.to_markdown(),
                model,
            )

            translator = deepl.Translator(auth_key=config.DEEPL_API_KEY)
            kor_summarization = translator.translate_text(summarization, target_lang="KO")

            return SuccessResponseModel(
                data={
                    "sql_query": result,
                    "data": results_df.to_dict(orient="records"),
                    "summarization": kor_summarization.text,
                }
            )
        else:
            # If the result is an error message, display it
            ErrorResponseModel(code="QUERY_ERROR", message=result)

    except Exception as e:
        return ErrorResponseModel(code="SERVER_ERROR", message=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
