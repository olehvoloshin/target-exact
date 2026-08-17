"""Exact target sink class, which handles writing streams."""


import ast
import base64
from singer_sdk.exceptions import FatalAPIError
import xmltodict
import json
import datetime
from pendulum import parse

from target_exact.client import ExactSink
from target_exact.constants import countries



class BuyOrdersSink(ExactSink):
    """Qls target sink class."""

    name = "BuyOrders"
    endpoint = "/purchaseorder/PurchaseOrders"

    def preprocess_record(self, record: dict, context: dict) -> dict:
        try:
            PurchaseOrderLines = []

            receipt_date = (
                record.get("created_at")
                if record.get("created_at")
                else datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                )
            )

            payload = {
                "OrderDate": record.get("transaction_date").strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
                "OrderNumber": record.get("id"),
                "Supplier": record.get("supplier_remoteId"),
                "PurchaseOrderLines": PurchaseOrderLines,
                "buy_order_remoteId": record.get("remoteId"),
            }

            if receipt_date:
                receipt_date = receipt_date.strftime(
                                "%Y-%m-%dT%H:%M:%S.%fZ"
                            )
                payload["ReceiptDate"] = receipt_date

            if "line_items" in record:
                record["line_items"] = json.loads(record["line_items"])
                for item in record["line_items"]:
                    line_item = {}
                    line_item["Item"] = item.get("product_remoteId")
                    if not item.get("lot_size") or item.get("lot_size") == False:
                        item["lot_size"] = 1
                    line_item["QuantityInPurchaseUnits"] = item.get("quantity") / item.get("lot_size", 1)
                    if receipt_date:
                        line_item["ReceiptDate"] = receipt_date

                    PurchaseOrderLines.append(line_item)

                return payload
            else:
                return None
        except Exception as e:
            return {"error": str(e)}

    def upsert_record(self, record: dict, context: dict) -> None:
        """Process the record."""
        endpoint = "/purchaseorder/PurchaseOrders"
        state_updates = dict()
        if record:
            if record.get("error"):
                raise Exception(record.get("error"))
            if record.get("buy_order_remoteId"):
                # TODO: Why is this block even here??
                id = record.get("buy_order_remoteId")
            else:
                del record['buy_order_remoteId']
                warehouse_uuid = self.config.get("warehouse_uuid")
                if warehouse_uuid:
                    record["Warehouse"] = warehouse_uuid
                else:
                    try:
                        warehouse_uuid = self.default_warehouse_uuid
                        record["Warehouse"] = warehouse_uuid
                    except Exception as e:
                        self.update_state(
                            {"error": "Warehouse uuid missing in config file"}
                        )
                        raise e
                response = self.request_api(
                    "POST", endpoint=endpoint, request_data=record
                )
                self.logger.info(f"response from api: {response.text}")
                res_json = xmltodict.parse(response.text)
                id = res_json["entry"]["content"]["m:properties"]["d:PurchaseOrderID"][
                    "#text"
                ]
                self.logger.info(f"{self.name} created with id: {id}")

            self.logger.info(f"Returning {id}, True, {state_updates}")
            return id, True, state_updates


class UpdateInventory(ExactSink):
    endpoint = "update_inventory"
    name = "UpdateInventory"
    endpoint = "UpdateInventory"

    def preprocess_record(self, record: dict, context: dict) -> None:
        return {}

    def upsert_record(self, record: dict, context: dict) -> None:
        state_updates = dict()
        id = "id"
        return id, True, state_updates



class SuppliersSink(ExactSink):
    """Qls target sink class."""

    name = "Suppliers"
    endpoint = "/crm/Accounts"

    def preprocess_record(self, record: dict, context: dict) -> dict:
        try:
            if record.get("division") and not self.current_division:
                self.endpoint = f"{record.get('division')}/{self.endpoint}"
                
            # make sure the record still exists, if not, create new record
            if record.get("id"):
                try:
                    self.request_api("GET", endpoint=f"{self.endpoint}(guid'{record['id']}')")
                except FatalAPIError as er:
                    if "Resource not found" in str(er):
                        self.logger.error(f"Supplier {record.get('id')} not found, gonna create new one")
                        record.pop("id")
                    else:
                        raise er

            if not record.get("id"):
                id = None
                if record.get("vendorCode"):
                    id = self.get_id("/crm/Accounts", {"$filter": f"Code eq '{record.get('vendorCode')}'"})
                if not id and record.get("vendorName"):
                    id = self.get_id("/crm/Accounts", {"$filter": f"Name eq '{self.escape_odata_string(record.get('vendorName'))}'"})
                if id:
                    record["id"] = id

            payload = {
                "Name": record.get("vendorName"),
                "Code": record.get("vendorCode"),
                "CodeAtSupplier": record.get("vendorNumber"),
                "IsSupplier": True,
                "PurchaseCurrency": record.get("currency"),
                "VATNumber": record.get("taxPayerNumber"),
                "PaymentConditionPurchase": record.get("paymentTerm"),
                "Id": record.get("id")
            }

            bankAccounts = record.get("bankAccounts")
            if bankAccounts:
                payload["bankAccounts"] = bankAccounts

            phones = record.get("phoneNumbers")
            if phones and isinstance(phones, str):
                phones = ast.literal_eval(phones) #TODO: Change this to json.loads
                if len(phones):
                    payload["Phone"] = phones[0]["number"]

            record_address = record.get("addresses")
            if record_address and isinstance(record_address, str):
                record_address = record_address.replace("null", '""')
                record_address = ast.literal_eval(record_address) # TODO: Change this to json.loads
                if len(record_address):
                    record_address = record_address[0]
                    payload["AddressLine1"] = record_address.get("line1")
                    payload["City"] = record_address.get("city")
                    payload["State"] = record_address.get("state")

                    country = record_address.get("country")
                    if country:
                        if len(country) == 2:
                            payload["Country"] = country
                        elif country in countries.keys():
                            payload["Country"] = countries[country]

            return payload
        except Exception as e:
            return {"error": str(e)}

    def upsert_record(self, record: dict, context: dict) -> None:
        """Process the record."""
        state_updates = dict()
        method = "POST"
        endpoint = self.endpoint
        action = "created"
        if record:
            if record.get("error"):
                raise Exception(record["error"])
            
            bankAccounts = record.pop("bankAccounts", None)
            # check if there is id to update or create the record
            id = record.pop("Id", None)
            if id:
                endpoint = f"{self.endpoint}(guid'{id}')"
                method = "PUT"
                state_updates = {"is_updated": True}
                action = "updated"
            # send request
            response = self.request_api(
                method, endpoint=endpoint, request_data=record
            )
            # send bank account data if exists
            if bankAccounts:
                for bankAccount in bankAccounts: # how not to send dupplicated bank accounts
                    bank_account = bankAccount.get("accountNumber")
                    # check if bankAccount already exists
                    bank_accounts_endpoint = f"/crm/BankAccounts?$filter=BankAccount eq '{self.escape_odata_string(bank_account)}' and Account eq '{id}'"
                    bank_acct = self.request_api("GET", endpoint=bank_accounts_endpoint)

                    if not bank_acct:
                        ba_payload = {
                            "BankAccount": bankAccount.get("accountNumber"),
                            "BankAccountHolderName": bankAccount.get("holderName"),
                            "BICCode": bankAccount.get("swiftCode")
                        }
                        bank_acct_response = self.request_api(
                            "POST", endpoint="/crm/BankAccounts", request_data=ba_payload
                        )
            # get new id if it's a new supplier else use id used for update
            if response.status_code == 201:
                res_json = xmltodict.parse(response.text)
                id = res_json["entry"]["content"]["m:properties"]["d:ID"]["#text"]
                self.logger.info(f"{self.name} {action} with id: {id}")
            return id, True, state_updates


class ProductsSink(ExactSink):
    """Qls target sink class."""

    name = "products"
    endpoint = "/logistics/Items"
    def search_product(self,product_code):
        id = None
        product_endpoint = f"/logistics/Items?$filter=Code eq '{product_code}'"
        product = self.request_api("GET", endpoint=product_endpoint)
        product_json = xmltodict.parse(product.text)
        products = product_json["feed"].get("entry")
        if products and len(products):
            if type(products) is dict:
                id = products["content"]["m:properties"]["d:ID"]["#text"]
            else:
                id = products[0]["content"]["m:properties"]["d:ID"]["#text"]
            
        return id
    def preprocess_record(self, record: dict, context: dict) -> dict:
        try:
            if record.get("division") and not self.current_division:
                self.endpoint = f"{record.get('division')}/{self.endpoint}"

            payload = {
                "Description": record.get("name"),
                "ExtraDescription": record.get("description"),
                "Code": record.get("sku",record.get("code")),
                "AverageCost": record.get("cost"),
                "IsSalesItem": True,# Indicate if the item is a sales item
                "IsPurchaseItem": True,# Indicate if the item is a purchase item
            }
            product_search = self.search_product(payload['Code'])
            if product_search:
                payload.update({"id":product_search})

            return payload
        except Exception as e:
            return {"error": str(e)}


    def upsert_record(self, record: dict, context: dict) -> None:
        """Process the record."""
        state_updates = dict()
        if record:
            if record.get("error"):
                raise Exception(record["error"])
            
            method_type = "POST"
            endpoint = self.endpoint
            if "id" in record:
                method_type = "PUT"
                endpoint = f"{endpoint}(guid'{record['id']}')"
                id = record['id']
                del record['id']
            try:
                response = self.request_api(
                    method_type, endpoint=endpoint, request_data=record
                )
                if response.status_code==204:
                    state_updates['updated'] = True
                    state_updates['existing'] = True
                    state_updates['success'] = True
                else:    
                    res_json = xmltodict.parse(response.text)
                    id = res_json["entry"]["content"]["m:properties"]["d:ID"]["#text"]
                self.logger.info(f"{self.name} created with id: {id}")
            except:
                raise KeyError
            return id, True, state_updates

class ItemsSink(ProductsSink):
    name = "Items"
class PurchaseInvoicesSink(ExactSink):
    """Qls target sink class."""

    name = "PurchaseInvoices"
    endpoint = "/purchase/PurchaseInvoices"
    def get_journal_code(self):
        code = None
        endpoint = (
            f"/financial/Journals?$filter=Description eq 'Purchase journal'"
        )
        response = self.request_api("GET", endpoint=endpoint)
        detail = xmltodict.parse(response.text)
        journals = detail["feed"].get("entry")
        if journals is not None:
            if type(journals) is dict:
                    code = journals["content"]["m:properties"]["d:Code"]
            else:
                code = journals[0]["content"]["m:properties"]["d:Code"]
            return code
        
    def preprocess_record(self, record: dict, context: dict) -> dict:
        try:
            if record.get("division") and not self.current_division:
                self.endpoint = f"{record.get('division')}/{self.endpoint}"

            payload = {
                "Currency": record.get("currency"),
                "DueDate": record.get("dueDate"),
                # "createdAt": record.get("InvoiceDate"),
                "Description": record.get("description"),
                "YourRef": record.get("invoiceNumber"),
                "InvoiceDate": record.get("createdAt"),
                "Type": record.get("type"),
                "Journal": record.get("journal"),
            }
            purchase_id = record.get("purchaseNumber")
            if purchase_id: 
                purchase_id = int(purchase_id)
                    
            if purchase_id and record.get("invoiceNumber"):
                payload.update({"YourRef": f"{purchase_id}-{record.get('invoiceNumber')}"})
            else:
                payload.update({"YourRef": record.get("invoiceNumber")})
            
            journal_code = self.get_journal_code()
            if journal_code:
                payload.update({"Journal":journal_code})
            supplier_endpoint = (
                f"/crm/Accounts?$filter=Name eq '{self.escape_odata_string(record.get('supplierName'))}'"
            )
            supplier = self.request_api("GET", endpoint=supplier_endpoint)
            supplier_json = xmltodict.parse(supplier.text)
            suppliers = supplier_json["feed"].get("entry")
            if suppliers and len(suppliers):
                if type(suppliers) is dict:
                    id = suppliers["content"]["m:properties"]["d:ID"]["#text"]
                else:
                    id = suppliers[0]["content"]["m:properties"]["d:ID"]["#text"]
                payload["Supplier"] = id
            else:
                return None

            invoice_lines = []
            lines = record.get("lineItems")
            if lines and isinstance(lines, str):
                lines = lines.replace("null", '""')
                lines = lines.replace("false", "False")
                lines = lines.replace("true", "True")
                lines = ast.literal_eval(lines) # TODO: Change to json.loads
                if len(lines):
                    for line in lines:
                        invoice_line = {
                            "UnitPrice": line.get("unitPrice"),
                            "Quantity": line.get("quantity"),
                            "Amount": line.get("totalPrice"),
                        }
                        discount = line.get("discount")
                        if discount is not None:
                            invoice_line.update({"Discount": discount})

                        if line.get("taxCode"):
                            invoice_line.update({"VATCode": line.get("taxCode")})

                        if line.get("taxAmount"):
                            invoice_line.update({"VATAmount": line.get("taxAmount")})    


                        product_endpoint = f"/logistics/Items?$filter=Description eq '{self.escape_odata_string(line.get('productName'))}'"
                        product = self.request_api("GET", endpoint=product_endpoint)
                        product_json = xmltodict.parse(product.text)
                        products = product_json["feed"].get("entry")
                        if products and len(products):
                            if type(products) is dict:
                                id = products["content"]["m:properties"]["d:ID"]["#text"]
                            else:
                                id = products[0]["content"]["m:properties"]["d:ID"]["#text"]
                            invoice_line["Item"] = id
                            invoice_lines.append(invoice_line)
                        else:
                            pass

                payload["PurchaseInvoiceLines"] = invoice_lines

            return payload
        except Exception as e:
            return {"error": str(e)}

    def upsert_record(self, record: dict, context: dict) -> None:
        """Process the record."""
        state_updates = dict()
        if record:
            if record.get("error"):
                raise Exception(record["error"])
            
            response = self.request_api(
                "POST", endpoint=self.endpoint, request_data=record
            )

            if response.status_code in [200,201]:
                state_updates["success"] = True

            res_json = xmltodict.parse(response.text)

            if "error" in res_json:
                id = None
                message = res_json["error"]["message"]["#text"]
                state_updates['error_response'] = message
                state_updates["success"] = False
                return None,False,state_updates

            if "entry" in res_json:
                id = res_json["entry"]["content"]["m:properties"]["d:ID"]["#text"]
                self.logger.info(f"{self.name} created with id: {id}")
                return id, True, state_updates

            return None, False, state_updates
        
class PurchaseEntriesSink(ExactSink):

    name = "PurchaseEntries"
    endpoint = "/purchaseentry/PurchaseEntries"

    def _find_existing_purchase_entry_id(self, invoice_number: str, supplier_id: str):
        """Return existing EntryID for the same YourRef+Supplier pair."""
        if not invoice_number or not supplier_id:
            return None

        params = {
            "$filter": (
                f"YourRef eq '{self.escape_odata_string(invoice_number)}' "
                f"and Supplier eq guid'{supplier_id}'"
            ),
            "$select": "EntryID,Modified",
            "$top": 1,
            "$orderby": "Modified desc",
        }
        response = self.request_api("GET", endpoint=self.endpoint, params=params)
        response_json = xmltodict.parse(response.text)
        entries = response_json.get("feed", {}).get("entry")
        if not entries:
            return None
        return entries["content"]["m:properties"]["d:EntryID"]["#text"]


    def _create_document(self, record_id=None):
        # check if document has already been created for the Entry
        if record_id:
            endpoint = f"{self.endpoint}(guid'{record_id}')"
            document_id = self.get_id(endpoint=endpoint, filter={}, key="Document")
            if document_id:
                self.logger.info(f"Document already found for entry {record_id}, appending new attachments to Document '{document_id}'")
        else:
            # Creates a document for the journal entry
            document_payload = {
                "Subject": "Journal Entry",
                "Type": "20",
            }

            document = self.request_api("POST", endpoint="/documents/Documents", request_data=document_payload)
            document_json = xmltodict.parse(document.text)
            document_id = document_json["entry"]["content"]["m:properties"]["d:ID"]["#text"]
        return document_id

    def _upload_attachment(self, attachments, record_id=None):
        """
        Checks if the file is a valid PDF file and uploads it to the API
        Gets all the files from the path set in config or the default path
        """
        input_path = self.config.get("input_path",'./')
        attachment_endpoint = "/documents/DocumentAttachments"

        if attachments:
            document_id = self._create_document(record_id)

        # fetch all attachments for the entry
        att_list = self.request_api("GET", endpoint=attachment_endpoint, params={"$filter": f"Document eq guid'{document_id}'"})
        att_list = xmltodict.parse(att_list.text)
        att_list = att_list.get("feed", {}).get("entry")

        if isinstance(att_list, dict):
            att_list = [att_list]

        existing_attachments = []
        if att_list:
            existing_attachments = [att["content"]["m:properties"]["d:FileName"] for att in att_list]

        for attachment in attachments:
            attachment_id = attachment.get("id")
            attachment_name = attachment.get("name")
            # some attachments are exported like {attachment_id}_{attachment_name} due to duplicated names
            if attachment_id:
                attachment_name = f"{attachment_id}_{attachment_name}"

            # check if attachment was previously sent, if so skip
            if not attachment_name or attachment_name in existing_attachments:
                self.logger.info(f"Attachment '{attachment_name}' already exist, skipping attachment...")
                continue

            input_path = f"{input_path}/" if not input_path.endswith("/") else input_path
            with open(f"{input_path}{attachment_name}", "rb") as f:
                attachment = f.read()
                attachment = base64.b64encode(attachment)

            attachment_payload = {
                "Attachment": attachment,
                "FileName": attachment_name,
                "Document": document_id,
            }

            attachment = self.request_api(
                "POST", endpoint=attachment_endpoint,
                request_data=attachment_payload
            )

            attachment_json = xmltodict.parse(attachment.text)
            attachment_id = attachment_json["entry"]["content"]["m:properties"]["d:ID"]["#text"]
        return document_id

    def preprocess_record(self, record: dict, context: dict) -> dict:
        try:
            if record.get("division") and not self.current_division:
                self.endpoint = f"{record.get('division')}/{self.endpoint}"

            transaction_date = record.get("transactionDate")
            period = None
            year = None
            if transaction_date:
                transac_date = parse(transaction_date)
                period = transac_date.month
                year = transac_date.year

            payload = {
                "Currency": record.get("currency"),
                "InvoiceNumber": record.get("number"),
                "YourRef": record.get("invoiceNumber"),
                "EntryDate": transaction_date,
                "Journal": record.get("journal"),
                "DueDate": record.get("dueDate"),
                "ReportingPeriod": period,
                "ReportingYear": year,
                "Description": record.get("description"),
                "PaymentReference": record.get("paymentReference"),
                "Id": record.get("id")
            }
            #get supplier id
            supplier_id = None
            
            # if provided supplierId - verify that supplier with this ID exists
            if supplierId := record.get("supplierId"):
                supplier_id = self.get_id("/crm/Accounts", {"$filter": f"ID eq guid'{supplierId}'"})
            
            if record.get("supplierCode") and not supplier_id:
                supplier_code = str(record.get("supplierCode"))
                # Exact stores Account Code as fixed-length (18) with leading spaces.
                normalized_code = supplier_code.rjust(18)
                supplier_id = self.get_id(
                    "/crm/Accounts",
                    {"$filter": f"Code eq '{self.escape_odata_string(normalized_code)}'"},
                )

            if not supplier_id:
                supplier_id = self.get_id("/crm/Accounts", {"$filter": f"Name eq '{self.escape_odata_string(record.get('supplierName'))}'"})
            
            if supplier_id:
                payload["Supplier"] = supplier_id
            else:
                return {"error": f"Unable to send PurchaseEntry as Supplier '{record.get('supplierName')}' doesn't exist for record with invoiceNumber {record.get('invoiceNumber')}"}

            # Update only when both invoice reference and supplier match an existing entry.
            if not payload.get("Id") and record.get("invoiceNumber") and supplier_id:
                existing_entry_id = self._find_existing_purchase_entry_id(
                    record.get("invoiceNumber"), supplier_id
                )
                if existing_entry_id:
                    self.logger.info(
                        "Found existing purchase entry '%s' for invoiceNumber '%s' and supplier '%s'.",
                        existing_entry_id,
                        record.get("invoiceNumber"),
                        supplier_id,
                    )
                    payload["Id"] = existing_entry_id

            lookup_taxes = self.config.get("lookup_taxes_by_name") or False
            invoice_lines = []
            lines = record.get("journalLines")
            if lines and isinstance(lines, str):
                lines = self.parse_objs(lines)
                if len(lines):
                    for line in lines:
                        #get gl account id
                        account_id = None
                        
                        # if provided accountId - verify that account with this ID exists
                        if accountId := record.get("accountId"):
                            account_id = self.get_id("/financial/GLAccounts", {"$filter": f"ID eq guid'{accountId}'"})
                        if line.get("accountNumber") and not account_id:
                            account_id = self.get_id("/financial/GLAccounts", {"$filter": f"Code eq '{line.get('accountNumber')}'"})
                        if not account_id:
                            account_id = self.get_id("/financial/GLAccounts", {"$filter": f"Description eq '{self.escape_odata_string(line.get('accountName'))}'"})
                        if not account_id:
                            return {"error": f"Unable to send PurchaseEntry as GL account {line.get('accountName')} doesn't exist for record with invoiceNumber {record.get('invoiceNumber')}"}
                        # flag added because new tenants are sending exact taxCode but older tenants are sending tax name as taxCode
                        if lookup_taxes:
                            vat_code = self.get_id("/vat/VATCodes", {"$filter": f"Description eq '{line.get('taxCode')}'"}, key="Code")
                        else:
                            vat_code = line.get('taxCode')
                        invoice_line = {
                            "AmountFC": line.get("amount"),
                            "AmountDC": line.get("amount"),
                            "GLAccount": account_id,
                            "Description": line.get("description", line.get("productName")),
                            "VATCode": vat_code,
                            "CostCenter": line.get("costCenter"),
                            "CostUnit": line.get("costUnit"),
                        }

                        # optional fields
                        if line.get('projectName'):
                            project_id = self.get_id("/project/Projects", {"$filter": f"Description eq '{self.escape_odata_string(line.get('projectName'))}'"})
                            if project_id:
                                invoice_line["Project"] = project_id
                        invoice_lines.append(invoice_line)

                payload["PurchaseEntryLines"] = invoice_lines
            payload = self.clean_payload(payload)
            if record.get("attachments"):
                record["attachments"] = json.loads(record["attachments"])
                if isinstance(record["attachments"], list) and record["attachments"]:
                    payload["Document"] = self._upload_attachment(record["attachments"], payload.get("Id"))
                else:
                    return {"error": "Attachments should be a list", "externalId": record.get("externalId")}
            
            return payload
        except Exception as e:
            return {"error": str(e)}

    def _get_existing_line_ids(self, entry_id: str) -> list:
        """Return the IDs of the PurchaseEntryLines currently attached to a PurchaseEntry."""
        response = self.request_api(
            "GET",
            endpoint="/purchaseentry/PurchaseEntryLines",
            params={"$filter": f"EntryID eq guid'{entry_id}'", "$select": "ID"},
        )
        response_json = xmltodict.parse(response.text)
        # an empty result parses as {"feed": None} (the "feed" key is present but its
        # value isn't a dict), so .get("feed", {}) alone doesn't protect against it
        entries = (response_json.get("feed") or {}).get("entry")
        if not entries:
            return []
        if isinstance(entries, dict):
            entries = [entries]
        return [entry["content"]["m:properties"]["d:ID"]["#text"] for entry in entries]

    def _replace_purchase_entry_lines(self, entry_id: str, new_lines: list) -> list:
        """Replace all PurchaseEntryLines for an existing PurchaseEntry.

        Exact's OData API does not support replacing the nested PurchaseEntryLines
        collection via a single PUT on the parent PurchaseEntries resource (embedding
        lines in a header PUT either errors or duplicates lines - see commit 9994f11,
        "fix payload for PUT purchase entries", 2024-01-02). Lines must instead be
        deleted and recreated individually through their own endpoint.

        Create the new lines BEFORE deleting the old ones. Exact rejects deleting a
        PurchaseEntry's last remaining line with "Unexpected number of lines. Should
        be at least one line." - confirmed live (job jvgkWM): an entry with 1 existing
        line failed on the very first DELETE, before any new line existed to replace
        it. Creating first means the entry always has >= 1 line at every point in
        time, at the cost of briefly showing both old and new lines together.
        """
        existing_line_ids = self._get_existing_line_ids(entry_id)

        created_ids = []
        try:
            for line in new_lines:
                line_payload = dict(line)
                line_payload["EntryID"] = entry_id
                response = self.request_api(
                    "POST", endpoint="/purchaseentry/PurchaseEntryLines", request_data=line_payload
                )
                line_json = xmltodict.parse(response.text)
                created_ids.append(line_json["entry"]["content"]["m:properties"]["d:ID"]["#text"])
        except Exception as e:
            raise Exception(
                f"Failed to create new PurchaseEntryLines for entry {entry_id} "
                f"(created {len(created_ids)}/{len(new_lines)} lines before failure - "
                f"old lines were left untouched, entry now has {len(existing_line_ids)} old "
                f"line(s) plus {len(created_ids)} new one(s) and needs manual review): {e}"
            )

        deleted_ids = []
        try:
            for line_id in existing_line_ids:
                self.request_api(
                    "DELETE", endpoint=f"/purchaseentry/PurchaseEntryLines(guid'{line_id}')"
                )
                deleted_ids.append(line_id)
        except Exception as e:
            raise Exception(
                f"Created {len(created_ids)} new PurchaseEntryLines for entry {entry_id} but "
                f"failed to delete the old ones (deleted {len(deleted_ids)}/{len(existing_line_ids)} "
                f"before failure - entry now has BOTH old and new lines and needs manual review): {e}"
            )
        return created_ids

    def upsert_record(self, record: dict, context: dict) -> None:
        """Process the record."""
        state_updates = dict()
        endpoint = self.endpoint
        action = "created"
        method = "POST"
        if record:
            if record.get("error"):
                raise Exception(record.get("error"))
            # check if there is id to update or create the record
            id = record.pop("Id", None)
            new_lines = record.pop("PurchaseEntryLines", None)
            if id:
                endpoint = f"{self.endpoint}(guid'{id}')"
                method = "PUT"
                action = "updated"
                state_updates["is_updated"] = True
            elif new_lines is not None:
                # Creating a new entry - lines are embedded in the single POST, as before.
                record["PurchaseEntryLines"] = new_lines
            
            try:
                response = self.request_api(
                    method, endpoint=endpoint, request_data=record
                )
            except Exception as e:
                # delete attachments if entry is new and posting failed
                if method == "POST" and record.get("Document"):
                    self.logger.info(f"Error happened while creating PurchaeEntry, deleting attachments associated with it...")
                    try:
                        response = self.request_api(
                            "DELETE", endpoint=f"/documents/Documents(guid'{record['Document']}')"
                        )
                    except Exception:
                        self.logger.info(f"Document '{record['Document']}' couldn't be deleted due to error {str(e)}.")

                    if response.status_code == 204:
                        self.logger.info(f"Document '{record['Document']}' succesfully deleted.")

                raise Exception(e)

            if response.status_code == 201:
                res_json = xmltodict.parse(response.text)
                id = res_json["entry"]["content"]["m:properties"]["d:EntryID"]["#text"]

            # Header PUT succeeded - now that we know the entry itself is valid, replace
            # its lines. If this fails, the header change is still kept (better than
            # silently keeping stale amounts, and it's already logged/raised below).
            if method == "PUT" and new_lines is not None:
                self._replace_purchase_entry_lines(id, new_lines)

            self.logger.info(f"{self.name} {action} with id: {id}")
            return id, True, state_updates
