# Engel & Völkers Germany - Master Structural Guide

## Overview
**Domain:** https://www.engelvoelkers.com/de/de  
**Language:** German (de-DE)  
**Industry:** Premium Real Estate  
**Coverage:** 1,000+ locations across 35+ countries

---

## PILLAR 1: Homepage (/de/de)

### Main Value Propositions
- **Find Your Dream Property** ("Finden Sie Ihre Traumimmobilie!")
- **Buying & Renting Options** - Toggle between Kaufen/Mieten
- **Sell Your Property** - "Immobilie verkaufen" section
- **Local Market Knowledge** - "Lokales Marktwissen"
- **Global Marketing Reach** - "Weltweite Vermarktung"

### Primary Navigation
```
├── Kaufen & Mieten (Buy & Rent)
│   ├── Immobilien zum Kauf finden
│   ├── Immobilien zur Miete finden
│   ├── Persönliches Suchprofil
│   ├── Käufer:in-Guide
│   ├── Immobilienfinanzierung
│   └── Budgetrechner
├── Immobilie verkaufen
│   ├── Verkaufen Sie Ihre Immobilie
│   ├── Bewerten Sie Ihre Immobilie
│   ├── Vermieten Sie Ihre Immobilie
│   └── Verkäufer:in-Guide
├── Immobilienmakler:in
│   ├── Standorte finden
│   └── Immobilienmakler:in werden
├── Commercial
│   ├── Gewerbeimmobilien
│   ├── Verkaufen & vermieten
│   └── Services
└── Unternehmen
    ├── Über Engel & Völkers
    ├── Karriere
    └── Presse
```

### Search Interface
- **Location:** Central hero section
- **Type Toggle:** Kaufen | Mieten (Buy | Rent)
- **Property Types:** Haus, Wohnung, Grundstück
- **Quick Links:** Popular cities (Hamburg, Berlin, München, etc.)

---

## PILLAR 2: Property Search (/immobilien/res/kaufen/immobilien)

### Search Filters
```
├── Location (Ort)
├── Property Type (Objekttyp)
│   ├── Haus (House)
│   ├── Wohnung (Apartment)
│   └── Grundstück (Land)
├── Price Range (Preis)
├── Rooms (Zimmer)
├── Living Space (Wohnfläche)
└── Features (Ausstattung)
```

### Property Cards Structure
```
├── Image Gallery (Primary + thumbnails)
├── Price (€ EUR)
├── Property Type & Location
├── Key Specs:
│   ├── Living Space (m²)
│   ├── Rooms
│   └── Plot Size (m²) - if applicable
├── Short Description
├── Reference Number (Objektnummer)
└── Contact/Details Button
```

### Pagination
- Infinite scroll or numbered pages
- Results counter ("1-12 von 450 Immobilien")
- Sort options: Price, Newest, Relevance

---

## PILLAR 3: Property Detail Page

### Image Gallery
- Main large image with thumbnails
- Full-screen lightbox option
- 360° tours (if available)

### Property Information
```
├── Basic Data (Grunddaten)
│   ├── Objektnummer (Reference)
│   ├── Objektart (Type)
│   ├── Nutzfläche (Usable area)
│   ├── Wohnfläche (Living space)
│   ├── Zimmer (Rooms)
│   ├── Schlafzimmer (Bedrooms)
│   └── Badezimmer (Bathrooms)
├── Location (Lage)
│   ├── Address
│   ├── Map integration
│   └── Neighborhood info
├── Description (Beschreibung)
├── Features (Ausstattung)
│   ├── Heating
│   ├── Construction year
│   ├── Condition
│   └── Special features
└── Energy Certificate (Energieausweis)
```

### Contact Form
```
├── Name (Vorname/Nachname)
├── Email
├── Phone
├── Message
├── Preferred contact time
└── Privacy consent checkbox
```

---

## PILLAR 4: Services & Tools

### 1. Property Valuation (/verkauf/immobilienbewertung)
- **Form Fields:**
  - Address
  - Property type
  - Living space
  - Plot size
  - Construction year
  - Condition
- **Output:** Free valuation report

### 2. Budget Calculator (/finanzierung/budgetrechner)
- **Inputs:**
  - Monthly income
  - Equity capital
  - Monthly expenses
- **Output:** Maximum purchase price

### 3. Financing (/finanzierung)
- Mortgage calculator
- Interest rate information
- Contact form for financing advice

### 4. Market Reports (/private-office-marktbericht)
- Regional market analysis
- Price trends
- Investment insights

---

## KEY INTERACTION POINTS FOR TARA

### 1. Search Flow
```
User Goal: "Find a house in Hamburg"
│
├── Step 1: Click search bar
├── Step 2: Type "Hamburg"
├── Step 3: Select "Haus" (House)
├── Step 4: Click "Kaufen" (Buy)
└── Step 5: View results / Apply filters
```

### 2. Property Inquiry Flow
```
User Goal: "Contact about property EV12345"
│
├── Step 1: Navigate to property page
├── Step 2: Click "Jetzt kontaktieren"
├── Step 3: Fill contact form
└── Step 4: Submit inquiry
```

### 3. Valuation Flow
```
User Goal: "Get property valuation"
│
├── Step 1: Navigate to /verkauf/immobilienbewertung
├── Step 2: Enter address
├── Step 3: Select property type
├── Step 4: Enter size/year
└── Step 5: Submit for valuation
```

---

## COMMON USER QUERIES

### Navigation Tasks
- "Zeige mir Häuser in Berlin" (Show houses in Berlin)
- "Finde eine Wohnung in München" (Find apartment in Munich)
- "Immobilienmakler in Hamburg finden" (Find agent in Hamburg)

### Information Tasks
- "Was kostet eine Bewertung?" (What does valuation cost?)
- "Wie funktioniert die Finanzierung?" (How does financing work?)
- "Wie viel ist meine Immobilie wert?" (What's my property worth?)

### Transaction Tasks
- "Ich möchte mein Haus verkaufen" (I want to sell my house)
- "Kontakt zu Makler aufnehmen" (Contact an agent)
- "Termin vereinbaren" (Schedule appointment)

---

## TECHNICAL NOTES

### URL Patterns
```
Search: /de/de/immobilien/res/{kaufen|mieten}/{objekttyp}/{region}/{stadt}
Property: /de/de/immobilien/res/{id}
Agent Search: /de/de/shops/{region}/{stadt}
Valuation: /de/de/verkauf/immobilienbewertung
```

### Key CSS Selectors
```css
/* Search Input */
input[placeholder*="Ort"]
input[data-testid="search-input"]

/* Property Cards */
[data-testid="property-card"]
.property-list-item

/* Filters */
.filter-sidebar
.filter-option

/* Contact Form */
.contact-form
input[name="email"]
textarea[name="message"]
```

### Dynamic Content
- Search results load via AJAX
- Filters update results dynamically
- Image galleries use lazy loading
- Cookie consent modal on first visit

---

## HIVE MIND INTEGRATION

### Key Pages to Index
1. Homepage structure and navigation
2. Search result patterns
3. Property detail layouts
4. Service pages (valuation, financing)
5. Regional landing pages (Berlin, Hamburg, etc.)

### GPS Hints for Common Tasks
```json
{
  "search_property": {
    "location": "Hero section or top navigation",
    "steps": ["Click search", "Enter location", "Select type", "Click search button"]
  },
  "contact_agent": {
    "location": "Property detail page right sidebar",
    "steps": ["Navigate to property", "Click 'Jetzt kontaktieren'", "Fill form"]
  },
  "property_valuation": {
    "location": "/verkauf/immobilienbewertung",
    "steps": ["Navigate to valuation page", "Enter property details", "Submit form"]
  }
}
```

---

## CONTENT EXTRACTION NOTES

### Priority Elements
1. **Navigation structure** - Primary and secondary menus
2. **Search functionality** - Input fields, filters, buttons
3. **Property listings** - Card structure, key data points
4. **Forms** - Contact forms, valuation forms
5. **FAQ sections** - Common questions and answers

### Text Content
- Property descriptions (often long, detailed)
- Service explanations
- Regional market descriptions
- Legal disclaimers and privacy info

### Media
- Property images (high-res galleries)
- Virtual tour embeds
- PDF downloads (market reports, guides)

---

*Generated for TARA Visual Co-Pilot Integration*
*Last Updated: 2026-02-24*
