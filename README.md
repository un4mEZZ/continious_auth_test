# Auth_test

Repository for testing **continuous authentication system based on user behavior analysis**.

### 1) Install requirements
from requirements.txt

### 2) Create a PostgreDB 

Tables' order:
db_name -> Schemas -> auth -> [4 tables]

Use pg scripts from **PostgreCreateTables.txt** in direct order.

Then connect this DB in main_app.py, e.g.:

```python
import ...

DATABASE_URL = "postgresql+asyncpg://postgres:1111@localhost:5432/behavioural_auth"
...
```
### 3) Run main_app.py & auth_service.py