from openai import OpenAI  

client = OpenAI()  

response = client.responses.create( 
    model="openai.gpt-oss-120b", 
    input=[ 
        {"role": "user", "content": "Write a one-sentence bedtime story about a unicorn."} 
    ] 
)  

print(response.output_text)
