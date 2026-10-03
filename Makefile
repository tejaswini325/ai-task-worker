.PHONY: test demo1 demo2 demo3
test:
	python -m pytest -q
demo1:  ## happy path: pagination, credit-note trap, 503s, ERP format validation, approval, verification
	python run.py "Find the latest invoice from Globex Corporation in the vendor portal, record it as a bill in our internal ERP, and tell me once it is done."
demo2:  ## ambiguity -> agent should ask_user
	python run.py "Record the latest Acme invoice in the ERP and tell me when it is done."
demo3:  ## answer 'n' at the approval prompt -> nothing written, status blocked
	python run.py "Find the latest invoice from Globex Corporation in the vendor portal and record it as a bill in our internal ERP."
