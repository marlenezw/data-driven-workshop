import azure.functions as func
import logging
import json

from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import AzureOpenAI
import os
from base64 import b64encode

client: AzureOpenAI
DEVELOPMENT = os.getenv("DEVELOPMENT", True)

if os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_KEY"):
    client = AzureOpenAI(
        api_version="2024-02-15-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
else:
    azure_credential = AzureCliCredential(tenant_id=os.getenv("AZURE_TENANT_ID"))
    token_provider = get_bearer_token_provider(azure_credential,
        "https://cognitiveservices.azure.com/.default")
    client = AzureOpenAI(
        api_version="2024-02-15-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        azure_ad_token_provider=token_provider
    )

completions_deployment = os.getenv("CHAT_DEPLOYMENT_NAME", "gpt-35-turbo")
embeddings_deployment = os.getenv("EMBEDDINGS_DEPLOYMENT_NAME", "text-embedding-ada-002")

if DEVELOPMENT:
    from backends.local import search_products
else:
    from backends.azure_cosmos import search_products

app = func.FunctionApp()

@app.blob_trigger(arg_name="imageblob", path="uploads",
                  connection="ImagesConnection") 
def image_trigger(imageblob: func.InputStream):
    logging.info(f"Python blob trigger function processing blob"
                f"Name: {imageblob.name}"
                f"Blob Size: {imageblob.length} bytes")
    # 1. Create an embedding from the file

    # 2. Save the embedding to the database

    # 3. Return the URL of the image and the embedding


def prep_search(query: str) -> str:
    """
    Generate a full-text search query for a SQL database based on a user question.
    Use SQL boolean operators if the user has been specific about what they want to exclude in the search.
    If the question is not in English, translate the question to English before generating the search query.
    If you cannot generate a search query, return just the number 0.
    """

    ### Start of implementation
    completion = client.chat.completions.create(
        model=completions_deployment,
        messages= [
        {
            "role": "system",
            "content": 
            """  
                Generate a full-text search query for a SQL database based on a user query. 
                Do not generate the whole SQL query; only generate string to go inside the MATCH parameter for FTS5 indexes. 
                Use SQL boolean operators if the user has been specific about what they want to exclude in the search.
                If the query is not in English, always translate the query to English.
                If you cannot generate a search query, return just the number 0.
            """
        },
        {   "role": "user",
            "content": f"Generate a search query for: A really nice winter jacket"
        },
        {  "role": "assistant",
            "content": "winter jacket"
        },
        {   "role": "user",
            "content": "Generate a search query for: 夏のドレス"
        },
        {   "role": "assistant",
            "content": "summer dress"
        },
        {
            "role": "user",
            "content": f"Generate a search query for: {query}"
        }],
        max_tokens=100, # maximum number of tokens to generate
        n=1, # return only one completion
        stop=None, # stop at the end of the completion
        temperature=0.3, # more predictable
        stream=False, # return the completion as a single string
        seed=1, # seed for reproducibility
    )
    search_query = completion.choices[0].message.content
    ### End of implementation
    return search_query

def fetch_embedding(input: str) -> list[float]:
    embedding = client.embeddings.create(
        input=input,
        model=embeddings_deployment,
    )
    return embedding.data[0].embedding

@app.route(methods=["post"], auth_level="anonymous",
                    route="search")
def search(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Python HTTP trigger function processed a request.")
    query = req.form.get('query')
    if not query:
        return func.HttpRequest(
            "{'error': 'Please pass a query on the query string or in the request body'}",
            status_code=400
        )

    fts_query = prep_search(query)
    embedding = fetch_embedding(query)
    sql_results = search_products(query, fts_query, embedding)

    return func.HttpResponse(json.dumps({
        "keywords": fts_query,
        "results": [product.model_dump() for product in sql_results]
        }
    ))


@app.route(methods=['post'], auth_level="anonymous",
           route="match")
def match(req: func.HttpRequest) -> func.HttpResponse:
    """
    Matches the image upload with the product in the database with the closest embedding.
    """
    image = req.files.get('image_upload')
    max_items = req.form.get('max_items', 2)
    if not image:
        return func.HttpResponse(
            "{'error': 'Please pass an image in the request body'}",
            status_code=400
        )
    image_contents = image.stream.read()
    image_type = image.mimetype

    base64_image = b64encode(image_contents).decode('utf-8')

    # 1. Ask the model to describe the image
    description = client.chat.completions.create(
        model=completions_deployment,
        messages= [
        {
            "role": "system",
            "content": 
            """  
                Generate a text description the clothes worn by the person in the image.
            """
        },
        {   
            "role": "user",
            "content": [
                { "type": "text", "content": "Describe the clothes in this image" },
                { "type": "image_url", "image_url": { "url": f"data:{image_type};base64,{base64_image}" } }
            ]
        }
        ],
        max_tokens=500, # maximum number of tokens to generate
        n=1, # return only one completion
        stop=None, # stop at the end of the completion
        temperature=0.3, # more predictable
        stream=False, # return the completion as a single string
        seed=1, # seed for reproducibility
    )
    image_description = description.choices[0].message.content
    text_embedding = fetch_embedding(image_description)

    # Do a product search with the text embedding
    sql_results = search_products(image_description, image_description, text_embedding)[:max_items]

    return func.HttpResponse(json.dumps({
        "keywords": image_description,
        "results": [product.model_dump() for product in sql_results],
        }))


@app.route(methods=["get"], auth_level="anonymous",
           route="seed_embeddings")
def seed_embeddings(req: func.HttpRequest) -> func.HttpResponse:
    # Seed the embeddings for the products in the database by calling the OpenAI API
    with open('data/test.json') as f:
        data = json.load(f)
        for product in data:
            if 'embedding' not in product or product['embedding'] is None:
                product['embedding'] = fetch_embedding(product['name'] + ' ' + product['description'])

        # Write the embeddings back to the test data
        with open('data/test.json', 'w') as f:
            json.dump(data, f)
                
        return func.HttpResponse("Successfully seeded embeddings")
