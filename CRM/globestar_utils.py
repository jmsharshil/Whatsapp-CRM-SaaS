import json
import logging
import os
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GLOBESTAR_PHONE_NUMBER_ID = "1145671108635369"  
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"


GLOBESTAR_PRODUCTS = [
    {
        "id": "1",
        "name": "AODD Pump",
        "features": [
            "✔ Air operated (no electricity needed)",
            "✔ Handles viscous & abrasive fluids",
            "✔ Self-priming",
            "✔ Dry run safe"
        ],
        "applications": [
            "• Slurry & chemicals",
            "• Solvent transfer",
            "• High viscous liquids"
        ],
        "specifications": [
            "• Capacity: Up to 45 m³/hr",
            "• Head: Up to 50 meters",
            "• Temperature: Up to 120°C",
            "• Material: PP / PVDF / SS",
            "• Type: Diaphragm"
        ]
    },
    {
        "id": "2",
        "name": "Chemical Process Pump",
        "features": [
            "✔ Handles corrosive chemicals safely",
            "✔ Heavy-duty industrial design",
            "✔ High efficiency & long service life",
            "✔ Available in multiple material options"
        ],
        "applications": [
            "• Chemical & petrochemical industries",
            "• Pharma & food processing",
            "• Dyes, solvents & acids handling",
            "• Paper & textile industries"
        ],
        "specifications": [
            "• Capacity: Up to 400 m³/hr",
            "• Head: Up to 150 meters",
            "• Pressure: Up to 16 bar",
            "• Temperature: Up to 200°C",
            "• Material: CI / CS / SS-304 / SS-316 / Alloy",
            "• Impeller: Closed / Semi-open / Open",
            "• Type: End suction / Back pull-out"
        ]
    },
    {
        "id": "3",
        "name": "Centrifugal Process Pump",
        "features": [
            "✔ High efficiency & smooth operation",
            "✔ Handles chemicals & liquids",
            "✔ Heavy-duty industrial design",
            "✔ Multiple material options"
        ],
        "applications": [
            "• Chemical industries",
            "• Pharma & food processing",
            "• Dyes & solvents"
        ],
        "specifications": [
            "• Capacity: Up to 400 m³/hr",
            "• Head: Up to 150 meters",
            "• Pressure: Up to 16 bar",
            "• Temperature: Up to 200°C",
            "• Material: CI / SS / Alloy",
            "• Type: End suction / Back pull-out"
        ]
    },
    {
        "id": "4",
        "name": "PP Centrifugal Pump",
        "features": [
            "✔ 100% corrosion resistant",
            "✔ Leak-proof design",
            "✔ Ideal for acid & chemical transfer",
            "✔ Long life performance"
        ],
        "applications": [
            "• Acid transfer (HCl, H2SO4)",
            "• Chemical plants",
            "• Gas scrubbing systems"
        ],
        "specifications": [
            "• Capacity: Up to 50 m³/hr",
            "• Head: Up to 50 meters",
            "• Temperature: Up to 70°C",
            "• Material: PP / UHMWPE",
            "• Type: Centrifugal"
        ]
    },
    {
        "id": "5",
        "name": "Portable Sewage Submersible Pump",
        "features": [
            "✔ Heavy-duty construction for tough conditions",
            "✔ Non-clog impeller for solid handling",
            "✔ Suitable for wastewater & effluent",
            "✔ Low maintenance & long service life"
        ],
        "applications": [
            "• STP (Sewage Treatment Plant)",
            "• ETP (Effluent Treatment Plant)",
            "• Municipal sewage systems",
            "• Industrial wastewater handling"
        ],
        "specifications": [
            "• Capacity: 10 – 500 m³/hr",
            "• Head: Up to 50 meters",
            "• Motor: 1 HP – 50 HP",
            "• Solid Handling: Up to 50 mm",
            "• Material: Cast Iron / SS",
            "• Type: Submersible / Non-clog"
        ]
    },
    {
        "id": "6",
        "name": "Self priming Mud Pump",
        "features": [
            "✔ Designed for handling thick & muddy fluids",
            "✔ Abrasion-resistant construction",
            "✔ Heavy-duty performance for harsh conditions",
            "✔ Reliable & long service life"
        ],
        "applications": [
            "• Drilling operations",
            "• Construction sites",
            "• Mining & excavation",
            "• Sludge & slurry handling"
        ],
        "specifications": [
            "• Capacity: 10 – 300 m³/hr",
            "• Head: Up to 70 meters",
            "• Solid Handling: High (mud / slurry)",
            "• Motor: 3 HP – 50 HP",
            "• Material: Cast Iron / Alloy Steel",
            "• Type: Horizontal / Submersible / Reciprocating"
        ]
    },
    {
        "id": "7",
        "name": "Dewatering Pump",
        "features": [
            "✔ High efficiency water removal",
            "✔ Suitable for dirty water",
            "✔ Rugged & portable",
            "✔ Low maintenance"
        ],
        "applications": [
            "• Construction sites",
            "• Mining & pits",
            "• Flood water"
        ],
        "specifications": [
            "• Capacity: 5 – 300 m³/hr",
            "• Head: Up to 60 meters",
            "• Motor: 1 HP – 30 HP",
            "• Material: CI",
            "• Type: Submersible"
        ]
    },
    {
        "id": "8",
        "name": "Vertical Multistage Inline Pump",
        "features": [
            "✔ Space-saving design",
            "✔ Easy installation",
            "✔ Low vibration",
            "✔ Energy efficient"
        ],
        "applications": [
            "• HVAC systems",
            "• Cooling towers",
            "• Water circulation"
        ],
        "specifications": [
            "• Capacity: 5 – 200 m³/hr",
            "• Head: Up to 100 meters",
            "• Material: CI / SS",
            "• Type: Inline centrifugal"
        ]
    },
    {
        "id": "9",
        "name": "Fire Fighting Pump and System",
        "features": [
            "✔ High pressure output",
            "✔ Reliable emergency operation",
            "✔ Heavy-duty build",
            "✔ Long life"
        ],
        "applications": [
            "• Commercial buildings",
            "• Factories",
            "• Fire safety systems"
        ],
        "specifications": [
            "• Capacity: As per system",
            "• Head: High pressure",
            "• Type: Centrifugal / Multistage",
            "• Standard: Fire safety compliant"
        ]
    },
    {
        "id": "10",
        "name": "Progressive cavity Screw Pump",
        "features": [
            "✔ Ideal for viscous liquids",
            "✔ Smooth flow without pulsation",
            "✔ High efficiency",
            "✔ Low noise"
        ],
        "applications": [
            "• Oil & fuel transfer",
            "• Chemical dosing",
            "• Food industry"
        ],
        "specifications": [
            "• Capacity: 1 – 150 m³/hr",
            "• Pressure: High pressure",
            "• Material: SS / CI",
            "• Type: Positive displacement"
        ]
    },
    {
        "id": "11",
        "name": "Pressure Booster Pump and System",
        "features": [
            "✔ Constant pressure supply",
            "✔ Automatic operation",
            "✔ Energy efficient",
            "✔ Compact system"
        ],
        "applications": [
            "• Buildings",
            "• Hotels",
            "• Industrial water supply"
        ],
        "specifications": [
            "• Capacity: 1 – 50 m³/hr",
            "• Head: Up to 60 meters",
            "• Motor: 0.5 – 10 HP",
            "• Type: Multistage"
        ]
    },
    {
        "id": "12",
        "name": "Lobe Pump",
        "features": [
            "✔ Ideal for viscous & shear-sensitive liquids",
            "✔ Gentle pumping (product quality maintain)",
            "✔ Hygienic & easy to clean (CIP/SIP compatible)",
            "✔ Low noise & smooth operation"
        ],
        "applications": [
            "• Food & beverage industry",
            "• Pharma & cosmetics",
            "• Dairy products (milk, cream, yogurt)",
            "• Syrups, pastes & viscous liquids"
        ],
        "specifications": [
            "• Capacity: 1 – 100 m³/hr",
            "• Pressure: Up to 10 bar",
            "• Viscosity: High viscosity handling",
            "• Temperature: Up to 150°C",
            "• Material: SS-304 / SS-316",
            "• Type: Positive displacement (Rotary lobe)"
        ]
    },
    {
        "id": "13",
        "name": "Rotary Gear Pump",
        "features": [
            "✔ Positive displacement for constant flow",
            "✔ Ideal for viscous & lubricating liquids",
            "✔ Compact & robust construction",
            "✔ Low maintenance & long service life"
        ],
        "applications": [
            "• Oil & fuel transfer",
            "• Lubrication systems",
            "• Chemical dosing",
            "• Bitumen & viscous liquids handling"
        ],
        "specifications": [
            "• Capacity: 1 – 200 m³/hr",
            "• Pressure: Up to 15 bar",
            "• Viscosity: High viscosity fluids",
            "• Temperature: Up to 200°C",
            "• Material: Cast Iron / SS / Alloy Steel",
            "• Type: Internal Gear / External Gear"
        ]
    },
    {
        "id": "14",
        "name": "Vertical Long Shaft Sump Pump",
        "features": [
            "✔ Ideal for deep sump & tank applications",
            "✔ Vertical design – space saving installation",
            "✔ Handles corrosive & contaminated liquids",
            "✔ Low maintenance with long service life"
        ],
        "applications": [
            "• Chemical & process industries",
            "• Sump / pit drainage",
            "• Effluent & wastewater handling",
            "• Tank transfer applications"
        ],
        "specifications": [
            "• Capacity: 5 – 200 m³/hr",
            "• Head: Up to 60 meters",
            "• Immersion Depth: Up to 10 meters (customizable)",
            "• Motor: 1 HP – 30 HP",
            "• Material: Cast Iron / SS / PP",
            "• Type: Vertical cantilever (long shaft)"
        ]
    },
    {
        "id": "15",
        "name": "Monoblock Pump",
        "features": [
            "✔ Compact and rigid design",
            "✔ High efficiency",
            "✔ Easy maintenance"
        ],
        "applications": [
            "• Domestic water supply",
            "• Irrigation",
            "• Industrial use"
        ],
        "specifications": [
            "• Capacity: Up to 100 m³/hr",
            "• Head: Up to 60 meters",
            "• Motor: 0.5 - 20 HP"
        ]
    }
]

def _meta_post_gs(payload: dict) -> bool:
    phone_id = GLOBESTAR_PHONE_NUMBER_ID
    if not phone_id or phone_id == "YOUR_GLOBESTAR_PHONE_ID_HERE":
        logger.error("[GLOBESTAR] GLOBESTAR_PHONE_NUMBER_ID is not set in code")
        return False

        
    token = settings.META_PERMANENT_TOKEN
    url = META_SEND_URL.format(phone_id=phone_id)
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[GLOBESTAR] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[GLOBESTAR] Meta API success: %s", r.text)
        
        try:
            resp_data = r.json()
            meta_id = ""
            if "messages" in resp_data and len(resp_data["messages"]) > 0:
                meta_id = resp_data["messages"][0].get("id", "")
                
            to_number = payload.get("to", "")
            msg_type = payload.get("type", "text")
            content = ""
            template_name = ""
            
            if msg_type == "text":
                content = payload.get("text", {}).get("body", "")
            elif msg_type == "template":
                template_name = payload.get("template", {}).get("name", "")
                content = f"[Template: {template_name}]"
            elif msg_type == "interactive":
                content = "[Interactive Message]"
            elif msg_type == "image":
                content = "[Image]"
            else:
                content = f"[{msg_type.capitalize()}]"
                
            from CRM.models import Customer, Conversation, Message, ClientAccount
            customer_obj, _ = Customer.objects.get_or_create(phone=to_number, defaults={'name': to_number})
            client_account_obj = ClientAccount.objects.filter(phone_number_id=phone_id).first()
            conv_obj, _ = Conversation.objects.get_or_create(
                customer=customer_obj, 
                phone_number_id=phone_id, 
                defaults={'client': client_account_obj}
            )
            
            # Map message type to choices in models.py
            db_msg_type = msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text'
            
            Message.objects.create(
                conversation=conv_obj,
                client=client_account_obj,
                customer=customer_obj,
                meta_message_id=meta_id,
                direction="outbound",
                message_type=db_msg_type,
                template_name=template_name,
                content=content,
                status='sent'
            )
        except Exception as e:
            logger.error("[GLOBESTAR] Error saving outbound message: %s", e)

        return True
    except Exception as exc:
        logger.error("[GLOBESTAR] Meta API exception: %s", exc)
        return False

def tpl_gs_welcome(to: str) -> bool:
    # Meta APIs often require the URL to explicitly end with .jpg or .png
    welcome_image_url = "https://picsum.photos/600/400.jpg"
    
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gs_welcome",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "image",
                            "image": {
                                "link": welcome_image_url
                            }
                        }
                    ]
                }
            ]
        },
    })

def tpl_gs_main_menu(to: str) -> bool:
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gs_main_menu",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "1"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "2"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "2",
                    "parameters": [{"type": "payload", "payload": "3"}],
                }
            ],
        },
    })

def tpl_gs_product_list(to: str) -> bool:
    rows = []
    for i, p in enumerate(GLOBESTAR_PRODUCTS[:9], start=1):
        rows.append({
            "id": p['id'],
            "title": f"{i}. {p['name']}"[:24]
        })
    
    # 10th row for Pagination
    rows.append({
        "id": "99",
        "title": "10. More Pumps ➡️"
    })
        
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {
                "type": "text",
                "text": "Industrial Pumps"
            },
            "body": {
                "text": "Please select a product from the list below:"
            },
            "footer": {
                "text": "Globe Star Engineers"
            },
            "action": {
                "button": "View Products",
                "sections": [
                    {
                        "title": "Pumps (1 to 9)",
                        "rows": rows
                    }
                ]
            }
        }
    })

def tpl_gs_product_list_page2(to: str) -> bool:
    rows = []
    for i, p in enumerate(GLOBESTAR_PRODUCTS[9:], start=10):
        rows.append({
            "id": p['id'],
            "title": f"{i}. {p['name']}"[:24]
        })
        
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {
                "type": "text",
                "text": "Industrial Pumps "
            },
            "body": {
                "text": "Please select from the remaining products:"
            },
            "footer": {
                "text": "Globe Star Engineers"
            },
            "action": {
                "button": "View More Products",
                "sections": [
                    {
                        "title": "Pumps (10 to 15)",
                        "rows": rows
                    }
                ]
            }
        }
    })

def send_gs_product_detail(to: str, product_id: str) -> bool:
    product = next((p for p in GLOBESTAR_PRODUCTS if p["id"] == product_id), None)
    if not product:
        return False
        
    # Set a default product image or add 'image' key in GLOBESTAR_PRODUCTS
    product_image_url = product.get("image", "https://picsum.photos/600/400.jpg")
    
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gs_product_detail",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "image",
                            "image": {
                                "link": product_image_url
                            }
                        }
                    ]
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": product['name']},
                        {"type": "text", "text": ", ".join(product['features'])},
                        {"type": "text", "text": ", ".join(product['applications'])},
                        {"type": "text", "text": ", ".join(product['specifications'])},
                    ]
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": product['id']}]
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "0"}]
                }
            ]
        }
    })

def send_gs_text(to: str, text: str) -> bool:
    return _meta_post_gs({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    })
