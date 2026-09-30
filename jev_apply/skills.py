"""Technology names as job descriptions and screening questions write them, with their aliases.

Used to (1) find the skills a job description mentions, so a run can ask you about the ones your profile has no
years for *before* the form asks, and (2) treat "React", "React.js" and "ReactJS" as one skill when a saved
answer is looked up, so one answer serves every wording.
"""

import re

# canonical name: aliases (lower case). A skill matches as a whole word ("Go" never matches "good").
VOCAB = {
    # .NET / Microsoft
    "C#": ["c#", "csharp", "c sharp"],
    ".NET": [".net", "dotnet", "dot net"],
    ".NET Core": [".net core"],
    ".NET Framework": [".net framework"],
    "ASP.NET": ["asp.net"],
    "ASP.NET Core": ["asp.net core"],
    "ASP.NET MVC": ["asp.net mvc", "mvc"],
    "Web API": ["web api", "webapi"],
    "Entity Framework": ["entity framework", "ef core"],
    "LINQ": ["linq"],
    "WPF": ["wpf"],
    "WinForms": ["winforms", "windows forms"],
    "Blazor": ["blazor"],
    "SQL Server": ["sql server", "mssql", "ms sql"],
    "Azure": ["azure", "microsoft azure"],
    "Azure DevOps": ["azure devops"],
    ".NET MAUI": ["maui", ".net maui"],
    "Xamarin": ["xamarin"],
    "SignalR": ["signalr"],
    "Dapper": ["dapper"],
    # languages
    "Python": ["python"],
    "Java": ["java"],
    "JavaScript": ["javascript"],
    "TypeScript": ["typescript"],
    "C++": ["c++", "cpp"],
    "C": ["c programming", "embedded c"],
    "Go": ["golang"],
    "Rust": ["rust"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "PHP": ["php"],
    "Ruby": ["ruby"],
    "Scala": ["scala"],
    "R": ["r programming"],
    "SQL": ["sql"],
    "Bash": ["bash", "shell scripting"],
    "PowerShell": ["powershell"],
    "MATLAB": ["matlab"],
    # web
    "React": ["react", "react.js", "reactjs"],
    "Angular": ["angular", "angularjs"],
    "Vue.js": ["vue", "vue.js", "vuejs"],
    "Node.js": ["node", "node.js", "nodejs"],
    "Next.js": ["next.js", "nextjs"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3"],
    "jQuery": ["jquery"],
    "REST APIs": ["rest api", "rest apis", "restful"],
    "GraphQL": ["graphql"],
    "gRPC": ["grpc"],
    "Microservices": ["microservices", "microservice"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Spring Boot": ["spring boot", "spring"],
    # data and cloud
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "SQLite": ["sqlite"],
    "Elasticsearch": ["elasticsearch"],
    "Cosmos DB": ["cosmos db", "cosmosdb"],
    "Kafka": ["kafka"],
    "RabbitMQ": ["rabbitmq"],
    "AWS": ["aws", "amazon web services"],
    "GCP": ["gcp", "google cloud"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "CI/CD": ["ci/cd", "cicd"],
    "Jenkins": ["jenkins"],
    "Git": ["git"],
    "Linux": ["linux"],
    "Spark": ["spark", "pyspark"],
    "Databricks": ["databricks"],
    "Snowflake": ["snowflake"],
    "Airflow": ["airflow"],
    "Power BI": ["power bi"],
    "Tableau": ["tableau"],
    # AI / ML
    "Artificial Intelligence": ["artificial intelligence", "ai"],
    "Machine Learning": ["machine learning", "ml"],
    "Deep Learning": ["deep learning"],
    "PyTorch": ["pytorch"],
    "TensorFlow": ["tensorflow"],
    "Keras": ["keras"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "XGBoost": ["xgboost"],
    "ONNX": ["onnx"],
    "Large Language Models": ["llm", "llms", "large language models"],
    "Generative AI": ["generative ai", "genai", "gen ai"],
    "LangChain": ["langchain"],
    "LangGraph": ["langgraph"],
    "LlamaIndex": ["llamaindex"],
    "RAG": ["rag", "retrieval-augmented generation", "retrieval augmented generation"],
    "Prompt Engineering": ["prompt engineering"],
    "AI Agents": ["ai agents", "agentic ai", "agentic"],
    "Hugging Face": ["hugging face", "huggingface"],
    "OpenAI API": ["openai"],
    "Vector Databases": ["vector database", "vector databases", "pinecone", "faiss", "chromadb"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "opencv"],
    "MLOps": ["mlops"],
    "Vertex AI": ["vertex ai"],
    "SageMaker": ["sagemaker"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    # industrial / IoT / embedded
    "SCADA": ["scada"],
    "PLC": ["plc", "plcs", "programmable logic controller"],
    "PLC Programming": ["plc programming", "ladder logic"],
    "HMI": ["hmi"],
    "DCS": ["dcs"],
    "OPC UA": ["opc ua", "opc-ua", "opcua"],
    "OPC DA": ["opc da"],
    "Modbus": ["modbus"],
    "DNP3": ["dnp3"],
    "IEC 61850": ["iec 61850"],
    "IEC 60870-5-104": ["iec 60870-5-104", "iec 104", "iec-104"],
    "BACnet": ["bacnet"],
    "MQTT": ["mqtt"],
    "Profinet": ["profinet"],
    "EtherCAT": ["ethercat"],
    "CAN Bus": ["can bus", "canopen", "can protocol"],
    "Siemens TIA Portal": ["tia portal"],
    "Siemens S7": ["s7-1200", "s7-1500", "siemens s7", "simatic"],
    "Allen-Bradley": ["allen bradley", "allen-bradley", "rockwell"],
    "Ignition": ["ignition scada", "inductive automation"],
    "WinCC": ["wincc"],
    "Industrial IoT": ["iiot", "industrial iot"],
    "IoT": ["iot", "internet of things"],
    "Edge Computing": ["edge computing"],
    "Embedded Systems": ["embedded systems", "embedded software", "firmware"],
    "RTOS": ["rtos", "freertos"],
    "Robotics": ["robotics", "ros", "ros2"],
    "MES": ["mes", "manufacturing execution"],
    "Android": ["android"],
    "BMS": ["bms", "building management system"],
    "Industrial Automation": ["industrial automation"],
}
# A one- or two-letter name ("C", "R", "Go") only matches through its explicit aliases ("golang", "c programming"):
# on its own it is an ordinary word ("go-getter").
ALIASES = {
    alias: name for name, aliases in VOCAB.items() for alias in aliases + ([name.lower()] if len(name) > 2 else [])
}


def canonical(phrase):
    """The vocabulary name for a skill phrase ('React.js' -> 'React'), or the phrase itself."""
    key = re.sub(r"\s+", " ", (phrase or "").lower()).strip(" ?*.:")
    key = re.sub(r"\s*\((programming language|framework|language)\)$", "", key)
    return ALIASES.get(key, key)


def prominent(title, description, limit=5):
    """The skills a job really asks for: named in its title, or at least twice in its description. Best first,
    at most `limit`. Screening questions ask about the few skills a poster tags, not every passing mention."""
    in_title = mentioned(title)
    low = (description or "").lower()
    counts = {}
    for alias, name in ALIASES.items():
        n = len(re.findall(rf"(?<![a-z0-9+#.]){re.escape(alias)}(?![a-z0-9+#-])", low))
        if n:
            counts[name] = counts.get(name, 0) + n
    ranked = sorted({*in_title, *counts}, key=lambda s: (s not in in_title, -counts.get(s, 0)))
    return [s for s in ranked if s in in_title or counts.get(s, 0) >= 2][:limit]


def mentioned(text):
    """Skills a job description names, by vocabulary name, in order of first mention."""
    low = (text or "").lower()
    spans = []
    for alias, name in ALIASES.items():
        for found in re.finditer(rf"(?<![a-z0-9+#.]){re.escape(alias)}(?![a-z0-9+#-])", low):
            spans.append((found.start(), found.end(), name))
    # A mention inside a longer skill's mention isn't a skill of its own: "SQL" in "SQL Server".
    kept = [s for s in spans if not any(o[0] <= s[0] and s[1] <= o[1] and o[1] - o[0] > s[1] - s[0] for o in spans)]
    return list(dict.fromkeys(name for _, _, name in sorted(kept)))


ASKED = re.compile(
    r"(?:how many )?years?(?: of)?(?: total| work| professional| hands[- ]on| relevant| practical)* experience "
    r"(?:do you have |have you |you have )?(?:with|in|using|on|of|working with|working on) ([^?*✱]+)",
    re.I,
)


def asked_skill(question):
    """The skill a 'How many years of experience with X?' question asks about, as a vocabulary name, or None."""
    found = ASKED.search(re.sub(r"\byear'?s'?\b", "years", question or "", flags=re.I))
    return canonical(found[1]) if found else None


def years_question(skill):
    """LinkedIn's wording of the years question for a skill (so a saved answer matches the real form)."""
    return f"How many years of work experience do you have with {skill}?"
