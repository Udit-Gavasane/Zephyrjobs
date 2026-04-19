"""
portals/workday.py — Workday Application Agent
===============================================
Navigates Workday job applications end to end:
  - Google SSO login with NYU Gmail
  - Multi-page form filling
  - File upload (resume PDF)
  - Email verification via gmail_handler
  - Issues flagging for unknown fields
  - Form state snapshot for replay
"""

import os
import sys
import json
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Page, BrowserContext

# Add parent dir to path so we can import db and gmail_handler
sys.path.insert(0, str(Path(__file__).parent.parent))
import db
from gmail_handler import GmailVerificationHandler, handle_email_verification_in_page

# ── CANDIDATE PROFILE ─────────────────────────────────────────────────────────
# Loaded from .env — these fill standard form fields automatically

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / '.env')

PROFILE = {
    "first_name":   "Udit",
    "last_name":    "Gavasane",
    "email":        os.getenv("APPLY_EMAIL", "udit.gavasane@nyu.edu"),
    "phone":        os.getenv("APPLY_PHONE", ""),
    "address_line1":    "525 50th Street Brooklyn",
    "city":             "New York",
    "state":            "New York",
    "postal_code":      "11220",
    "country":          "United States of America",
    "phone_type":       "Mobile",
    "linkedin":     "www.linkedin.com/in/udit-gavasane",
    "github":       "www.github.com/Udit-Gavasane",
    "location":     "New York, NY",
    "work_auth":    "Yes",          # Are you authorized to work in the US?
    "sponsorship":  "No",           # Will you require sponsorship now?
    "salary_min":   "60000",
    "salary_max":   "120000",
    "notice_period": "Immediately",
    "experience_years": "2",
}

# Answers to common screening questions — add more as you encounter them
SCREENING_ANSWERS = {
    "authorized to work":           "Yes",
    "phone device type":            "Home",
    "device type":                  "Home",
    "phone type":                   "Home",
    "country":                      "United States of America",
    "state":                        "New York",
    "require sponsorship":          "No",
    "require visa":                 "No",
    "willing to relocate":          "Yes",
    "how did you hear about us":    "Linkedin Jobs",
    "how did you hear":             "Linkedin Jobs",
    "previously worked for nvidia": "No",
    "worked for nvidia":            "No",
    "employee or contractor":       "No",
    "years of experience":          "2",
    "highest level of education":   "Masters",
    "gender":                       "Prefer not to say",
    "veteran":                      "I am not a protected veteran",
    "disability":                   "I don't wish to answer",
    "race":                         "Prefer not to disclose",
    "ethnicity":                    "Prefer not to disclose",
}

# Session storage path — saves Google login so we don't re-auth every time
SESSIONS_DIR = Path(__file__).parent.parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
GOOGLE_SESSION = SESSIONS_DIR / "google_session.json"


# ── WORKDAY AGENT ─────────────────────────────────────────────────────────────

class WorkdayAgent:
    def __init__(self, job: dict, application_id: str, resume_pdf_path: str = None):
        self.job = job
        self.application_id = application_id
        self.resume_pdf_path = resume_pdf_path
        self.gmail = GmailVerificationHandler()
        self.form_snapshot = {}   # saves all filled values for replay
        self.page_number = 0
        self.issues = []

    async def run(self) -> bool:
        """
        Main entry point. Returns True if application submitted successfully.
        """
        db.log("info", "WORKDAY", f"Starting application for {self.job['company']}", self.job['id'])

        async with async_playwright() as pw:
            # Launch visible browser so we can debug — set headless=True for overnight runs
            browser = await pw.chromium.launch(headless=False, slow_mo=100)

            # Load saved Google session if it exists
            if GOOGLE_SESSION.exists():
                db.log("info", "WORKDAY", "Loading saved Google session")
                context = await browser.new_context(storage_state=str(GOOGLE_SESSION))
            else:
                db.log("info", "WORKDAY", "No saved session — will need Google login")
                context = await browser.new_context()

            page = await context.new_page()

            try:
                success = await self._apply(page, context)

                # Save Google session after successful auth
                await context.storage_state(path=str(GOOGLE_SESSION))
                db.log("info", "WORKDAY", "Google session saved for future runs")

                return success

            except Exception as e:
                db.log("error", "WORKDAY", f"Unhandled error: {e}", self.job['id'])
                raise
            finally:
                await browser.close()

    async def _apply(self, page: Page, context: BrowserContext) -> bool:
        """Full application flow."""

        # ── Step 1: Navigate to job URL ──
        db.log("info", "WORKDAY", f"Navigating to {self.job['url']}")
        await page.goto(self.job['url'], wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # ── Step 2: Click Apply ──
        applied = await self._click_apply(page)
        if not applied:
            db.log("warn", "WORKDAY", "Could not find Apply button")
            return False

        await page.wait_for_timeout(2000)

        # Handle "Start Your Application" modal
        await self._handle_start_modal(page)
        await page.wait_for_timeout(1000)

        # ── Step 3: Handle Google SSO ──
        signed_in = await self._handle_google_sso(page, context)
        if not signed_in:
            db.log("warn", "WORKDAY", "Google SSO failed — flagging for manual")
            self._flag_issue(page, 0, "Google Sign-In required", "auth", None)
            return False

        await page.wait_for_timeout(3000)

        # ── Step 4: Navigate multi-page form ──
        max_pages = 10
        while self.page_number < max_pages:
            db.log("info", "WORKDAY", f"Processing form page {self.page_number + 1}")

            # Save snapshot of current URL/page
            self.form_snapshot[f"page_{self.page_number}_url"] = page.url

            # Fill all fields on this page
            await self._fill_page(page)

            # Check if we hit issues on this page
            if self.issues:
                db.log("warn", "WORKDAY",
                    f"{len(self.issues)} issues flagged — pausing for manual resolution")
                await self._save_issues()
                db.update_job_status(self.job['id'], 'flagged')
                db.update_application(self.application_id, {
                    'status': 'flagged',
                    'form_snapshot': self.form_snapshot
                })
                return False

            # Try to go to next page
            next_clicked = await self._click_next(page)

            if not next_clicked:
                # No next button — try submit
                submitted = await self._click_submit(page)
                if submitted:
                    db.log("success", "WORKDAY",
                        f"Application submitted for {self.job['company']}!", self.job['id'])
                    db.update_job_status(self.job['id'], 'applied')
                    db.update_application(self.application_id, {
                        'status': 'applied',
                        'applied_at': datetime.now(timezone.utc).isoformat(),
                        'form_snapshot': self.form_snapshot,
                    })
                    return True
                else:
                    db.log("warn", "WORKDAY", "Neither Next nor Submit found on page")
                    break

            self.page_number += 1
            await page.wait_for_timeout(2000)

        db.log("error", "WORKDAY", "Max pages reached without submitting", self.job['id'])
        return False

    async def _click_apply(self, page: Page) -> bool:
        """Find and click the Apply button on the job posting page."""
        selectors = [
                    "a[data-automation-id='applyButton']",
                    "button[data-automation-id='applyButton']",
                    "a:has-text('Apply')",
                    "button:has-text('Apply')",
                    "a:has-text('Apply Now')",
                    "button:has-text('Apply Now')",
                ]
        for sel in selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000)
                if btn:
                    await btn.click()
                    db.log("info", "WORKDAY", "Clicked Apply button")
                    return True
            except Exception:
                continue
        return False

    async def _handle_start_modal(self, page: Page):
        """Click 'Apply Manually' if Workday shows the application method modal."""
        try:
            # Wait longer for modal to fully render
            await page.wait_for_timeout(2000)

            # Try multiple selectors for the Apply Manually button
            selectors = [
                "button:has-text('Apply Manually')",
                "a:has-text('Apply Manually')",
                "[data-automation-id='applyManually']",
                "button:has-text('Manually')",
            ]
            for sel in selectors:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        db.log("info", "WORKDAY", "Clicked Apply Manually on modal")
                        await page.wait_for_timeout(2000)
                        return
                except Exception:
                    continue

            # Last resort — find by evaluating all buttons text
            clicked = await page.evaluate("""
                () => {
                    const buttons = Array.from(document.querySelectorAll('button, a'));
                    const btn = buttons.find(b => b.textContent.trim().includes('Apply Manually'));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """)
            if clicked:
                db.log("info", "WORKDAY", "Clicked Apply Manually via JS evaluate")
                await page.wait_for_timeout(2000)
            else:
                db.log("warn", "WORKDAY", "Apply Manually button not found")

        except Exception as e:
            db.log("warn", "WORKDAY", f"Modal handling error: {e}")

    async def _handle_google_sso(self, page: Page, context: BrowserContext) -> bool:
        """
        Handle Google Sign-In on Workday.
        First run: clicks Google SSO, fills email, then waits for you to
        complete Microsoft SSO + Duo manually. Saves session after.
        Subsequent runs: session already saved, skips login entirely.
        """
        await page.wait_for_timeout(2000)

        # Check if modal is still open
        on_job_page = await page.query_selector("button:has-text('Apply Manually')")
        if on_job_page:
            db.log("warn", "WORKDAY", "Modal still open — clicking Apply Manually again")
            await on_job_page.click()
            await page.wait_for_timeout(2000)

        # Check if sign-in page is showing
        sign_in_visible = await page.query_selector(
            "button:has-text('Sign in with Google'), button:has-text('Sign in with email')"
        )
        if not sign_in_visible:
            db.log("info", "WORKDAY", "Already authenticated")
            return True

        db.log("info", "WORKDAY", "Sign in page detected — clicking Google SSO")

        # Click Sign in with Google
        google_selectors = [
            "button:has-text('Sign in with Google')",
            "a:has-text('Sign in with Google')",
            "[data-automation-id='googleSignIn']",
        ]

        for sel in google_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    db.log("info", "WORKDAY", "Clicked Sign in with Google")
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        # Fill NYU email if Google email field appears
        try:
            email_field = await page.wait_for_selector(
                "input[type='email']", timeout=8000
            )
            if email_field:
                await email_field.fill(PROFILE["email"])
                await page.keyboard.press("Enter")
                db.log("info", "WORKDAY", f"Filled email: {PROFILE['email']}")
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        # At this point Microsoft SSO + Duo appears
        # Wait up to 3 minutes for you to complete it manually
        db.log("info", "WORKDAY", "=== ACTION REQUIRED ===")
        db.log("info", "WORKDAY", "Complete Microsoft login + Duo in the browser window")
        db.log("info", "WORKDAY", "Waiting up to 3 minutes...")

        try:
            # Wait until we're past the sign-in page — detect by absence of sign-in elements
            await page.wait_for_function(
                """() => {
                    const url = window.location.href;
                    return url.includes('myworkdayjobs.com') &&
                           !document.querySelector('input[type="email"]') &&
                           !document.querySelector('input[type="password"]') &&
                           !document.querySelector('[id*="duo"]') &&
                           !document.title.toLowerCase().includes('sign in');
                }""",
                timeout=180000
            )
            db.log("info", "WORKDAY", "Login completed — continuing application")
            return True
        except Exception:
            db.log("error", "WORKDAY", "Login timeout — did not complete in 3 minutes")
            return False

    async def _handle_email_login(self, page: Page) -> bool:
        """Fallback: fill email/password login if no Google SSO."""
        try:
            email_field = await page.wait_for_selector("input[type='email']", timeout=5000)
            if email_field:
                await email_field.fill(PROFILE["email"])
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)

                # Check for email verification
                verified = await handle_email_verification_in_page(
                    page, self.gmail, "workday", timeout=90
                )
                return verified
        except Exception:
            pass
        return False

    async def _fill_page(self, page: Page):
        """Fill all fields on current page generically using exact Workday field IDs."""
        await page.wait_for_timeout(1000)

        # ── How Did You Hear (multiselect search) ──
        try:
            source = await page.query_selector("input#source--source")
            if source and await source.is_visible():
                desc_id = await source.get_attribute("aria-describedby")
                already = False
                if desc_id:
                    desc = await page.query_selector(f"[id='{desc_id}']")
                    if desc and "linkedin" in (await desc.inner_text()).lower():
                        already = True
                if not already:
                    await source.click()
                    await source.fill("Linkedin")
                    await page.wait_for_timeout(800)
                    await source.press("Enter")
                    await page.wait_for_timeout(500)
                    db.log("info", "WORKDAY", "Filled 'How Did You Hear' = 'Linkedin Jobs'")
        except Exception as e:
            db.log("warn", "WORKDAY", f"How Did You Hear error: {e}")

        # ── Click Add buttons only if work/education fields aren't already visible ──
        try:
            job_title = await page.query_selector("input[name='jobTitle']")
            if not job_title or not await job_title.is_visible():
                # Only click Add buttons inside Work Experience and Education sections
                clicked = await page.evaluate("""
                    () => {
                        const sections = ['Work-Experience-section', 'Education-section'];
                        let count = 0;
                        sections.forEach(sectionId => {
                            const section = document.getElementById(sectionId);
                            if (!section) return;
                            const group = section.closest('[role="group"]') || section.parentElement;
                            if (!group) return;
                            const btns = group.querySelectorAll('button[data-automation-id="add-button"]');
                            btns.forEach(btn => {
                                if (btn.textContent.trim() === 'Add') {
                                    btn.click();
                                    count++;
                                }
                            });
                        });
                        return count;
                    }
                """)
                if clicked:
                    await page.wait_for_timeout(1500)
                    db.log("info", "WORKDAY", f"Clicked {clicked} Add buttons")
        except Exception as e:
            db.log("warn", "WORKDAY", f"Add button error: {e}")

        # ── Field of Study (multiselect search) ──
        try:
            fos = await page.query_selector("input#education-19--fieldOfStudy, input[id*='fieldOfStudy']")
            if fos and await fos.is_visible():
                desc_id = await fos.get_attribute("aria-describedby")
                already = False
                if desc_id:
                    desc = await page.query_selector(f"[id='{desc_id}']")
                    if desc and "0 items" not in (await desc.inner_text()):
                        already = True
                if not already:
                    await fos.click()
                    await fos.fill("Computer Science")
                    await page.wait_for_timeout(1000)
                    await fos.press("Enter")
                    await page.wait_for_timeout(800)
                    clicked = await page.evaluate("""
                        () => {
                            const opts = document.querySelectorAll("li[role='option'], [data-automation-id='promptOption']");
                            const el = Array.from(opts).find(o => o.textContent.trim() === 'Computer Science');
                            if (el) { el.click(); return true; }
                            return false;
                        }
                    """)
                    if clicked:
                        db.log("info", "WORKDAY", "Filled 'Field of Study' = 'Computer Science'")
        except Exception as e:
            db.log("warn", "WORKDAY", f"Field of Study error: {e}")

        # ── Text inputs by exact field name ──
        field_map = {
            "legalName--firstName":    PROFILE["first_name"],
            "legalName--lastName":     PROFILE["last_name"],
            "addressLine1":            PROFILE["address_line1"],
            "city":                    PROFILE["city"],
            "postalCode":              PROFILE["postal_code"],
            "phoneNumber":             PROFILE["phone"],
            "extension":               None,  # skip
            # Work Experience
            "jobTitle":                "Software Engineer 2",
            "companyName":             "Dell Technologies",
            "location":                "Bangalore, India",
            "roleDescription":         "Implemented Java automation frameworks for distributed backend systems. Developed API validation and performance tests reducing response times by 80%. Authored 2000+ test cases using Selenium WebDriver, Python, and Maven reducing manual testing effort by 40 hours.",
            # Education
            "schoolName":              "New York University",
            "fieldOfStudy":            "Computer Science",
            "gradeAverage":            "3.8",
            # Self Identify
            "name":                    f"{PROFILE['first_name']} {PROFILE['last_name']}",
            "employeeId":              "",
            # Social
            "linkedInAccount":         PROFILE["linkedin"],
        }

        for name, value in field_map.items():
            if value is None:
                continue
            try:
                field = await page.query_selector(f"input[name='{name}'], textarea[name='{name}']")
                if not field or not await field.is_visible():
                    continue
                current = await field.input_value()
                if current and name not in ["name"]:  # always overwrite name on self-identify
                    continue
                if value == "":
                    continue
                await field.fill(str(value))
                self.form_snapshot[name] = value
                db.log("info", "WORKDAY", f"Filled '{name}' = '{value}'")
            except Exception as e:
                db.log("warn", "WORKDAY", f"Error filling '{name}': {e}")

        # ── Date fields ──
        # Fill dates using JS directly on spinbutton inputs
        date_js = await page.evaluate("""
            () => {
                const results = [];
                const allInputs = document.querySelectorAll('input[role="spinbutton"]');
                allInputs.forEach(input => {
                    const id = input.id || '';
                    const label = input.getAttribute('aria-label') || '';
                    results.push({id, label});
                });
                return results;
            }
        """)
        db.log("info", "WORKDAY", f"Spinbutton fields found: {date_js}")

        # Fill date fields — MM input then Tab to YYYY input
        date_values = {
            "startDate": ("01", "2023"),   # work ex start: Jan 2023
            "endDate":   ("08", "2024"),   # work ex end: Aug 2024
            "firstYear": (None, "2024"),   # education start: 2024 (year only)
            "lastYear":  (None, "2026"),   # education end: 2026 (year only)
        }
        for spin in date_js:
            spin_id = spin['id']
            spin_label = spin['label']  # 'Month' or 'Year'

            month_val = None
            year_val = None

            for key, (m, y) in date_values.items():
                if key in spin_id:
                    month_val = m
                    year_val = y
                    break

            if not month_val and not year_val:
                continue

            value = month_val if spin_label == 'Month' else year_val
            if not value:
                continue

            try:
                # Focus via JS to avoid viewport issues, then type via keyboard
                await page.evaluate(f"document.getElementById('{spin_id}').focus()")
                await page.wait_for_timeout(200)
                await page.keyboard.type(value, delay=100)
                await page.wait_for_timeout(200)
                db.log("info", "WORKDAY", f"Filled date '{spin_id}' = '{value}'")
            except Exception as e:
                db.log("warn", "WORKDAY", f"Date field error '{spin_id}': {e}")

        # ── Self-identify date (today's date) ──
        try:
            from datetime import date
            today = date.today()
            self_date_fields = {
                "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input": str(today.month),
                "selfIdentifiedDisabilityData--dateSignedOn-dateSectionDay-input": str(today.day),
                "selfIdentifiedDisabilityData--dateSignedOn-dateSectionYear-input": str(today.year),
            }
            for field_id, value in self_date_fields.items():
                try:
                    await page.evaluate(f"document.getElementById('{field_id}').focus()")
                    await page.wait_for_timeout(200)
                    await page.keyboard.type(value, delay=100)
                    await page.wait_for_timeout(200)
                    db.log("info", "WORKDAY", f"Filled self-identify date '{field_id}' = '{value}'")
                except Exception:
                    pass
        except Exception as e:
            db.log("warn", "WORKDAY", f"Self-identify date error: {e}")

        # ── Disability checkbox — "No, I do not have a disability" ──
        try:
            disability_clicked = await page.evaluate("""
                () => {
                    const checkboxes = document.querySelectorAll('input[id*="disabilityStatus"]');
                    for (const cb of checkboxes) {
                        const label = cb.closest('div')?.nextElementSibling?.textContent?.trim() ||
                                      cb.parentElement?.parentElement?.textContent?.trim() || '';
                        if (label.includes('No, I do not have a disability')) {
                            if (!cb.checked) cb.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if disability_clicked:
                db.log("info", "WORKDAY", "Checked disability: No, I do not have a disability")
        except Exception as e:
            db.log("warn", "WORKDAY", f"Disability checkbox error: {e}")

        # ── Button dropdowns ──
        button_map = {
            "countryRegion":  "New York",
            "phoneType":      "Home",
            "degree":         "Masters",
        }
        buttons = await page.query_selector_all("button[aria-haspopup='listbox']")
        for btn in buttons:
            try:
                auto_id = await btn.get_attribute("data-automation-id") or ""
                if auto_id == "utilityMenuButton":
                    continue
                btn_name = await btn.get_attribute("name") or ""
                btn_text = (await btn.inner_text()).strip()
                if btn_text and btn_text != "Select One":
                    continue
                value = button_map.get(btn_name, "")
                if not value:
                    aria = (await btn.get_attribute("aria-label") or "").lower()
                    if "ethnicity" in aria:
                        value = "Asian (Not Hispanic or Latino) (United States of America)"
                    elif "gender" in aria:
                        value = "Male"
                    elif "veteran" in aria:
                        value = "I AM NOT A VETERAN"
                    elif "authorized to work" in aria or "legally authorized" in aria:
                        value = "Yes"
                    elif "sponsorship" in aria or "visa status" in aria:
                        value = "No"
                    else:
                        # Try reading legend text for questionnaire dropdowns
                        legend_text = await page.evaluate("""
                            (btn) => {
                                const fieldset = btn.closest('fieldset');
                                if (!fieldset) return '';
                                const legend = fieldset.querySelector('legend');
                                return legend ? legend.innerText.toLowerCase() : '';
                            }
                        """, btn)
                        if "authorized to work" in legend_text or "legally authorized" in legend_text:
                            value = "Yes"
                        elif "sponsorship" in legend_text or "visa status" in legend_text:
                            value = "No"
                        else:
                            for k, v in button_map.items():
                                if k in aria or k in btn_name:
                                    value = v
                                    break
                if not value:
                    continue
                await btn.click()
                await page.wait_for_timeout(800)

                # Click option via JS — handles apostrophes and scrolling
                clicked = await page.evaluate(f"""
                    () => {{
                        const opts = document.querySelectorAll("li[role='option'], [data-automation-id='promptOption']");
                        const el = Array.from(opts).find(o => o.textContent.trim() === `{value}`);
                        if (el) {{ el.click(); return true; }}
                        return false;
                    }}
                """)
                if clicked:
                    self.form_snapshot[btn_name] = value
                    db.log("info", "WORKDAY", f"Dropdown '{btn_name}' = '{value}'")
                else:
                    await page.keyboard.press("Escape")
                    db.log("warn", "WORKDAY", f"Option '{value}' not found for '{btn_name}'")
            except Exception as e:
                db.log("warn", "WORKDAY", f"Button dropdown error: {e}")

        # ── Radio buttons ──
        radio_map = {
            "candidateIsPreviousWorker": "false",
        }
        for name, value in radio_map.items():
            try:
                radio = await page.query_selector(
                    f"input[type='radio'][name='{name}'][value='{value}']"
                )
                if not radio:
                    continue
                is_checked = await radio.is_checked()
                if is_checked:
                    continue
                radio_id = await radio.get_attribute("id")
                label = await page.query_selector(f"label[for='{radio_id}']")
                if label:
                    await label.click()
                else:
                    await radio.click()
                db.log("info", "WORKDAY", f"Radio '{name}' = '{value}'")
            except Exception as e:
                db.log("warn", "WORKDAY", f"Radio error: {e}")

        # ── Terms and Conditions checkbox ──
        try:
            terms = await page.query_selector("input[name='acceptTermsAndAgreements']")
            if terms and await terms.is_visible():
                if not await terms.is_checked():
                    await terms.click()
                    db.log("info", "WORKDAY", "Checked Terms and Conditions")
        except Exception as e:
            db.log("warn", "WORKDAY", f"Terms checkbox error: {e}")

        # ── Resume upload ──
        await self._upload_resume(page)

    async def _fill_text_fields(self, page: Page):
        """Fill all text input fields using Workday-specific selectors."""
        # Workday text inputs always have id with '--' pattern
        fields = await page.query_selector_all(
            "input[type='text']:not([class*='css-77hcv']):not([data-uxi-widget-type='selectinput']), "
            "input[type='tel'], "
            "input[aria-required]"
        )

        for field in fields:
            try:
                is_visible = await field.is_visible()
                if not is_visible:
                    continue

                current_val = await field.input_value()
                if current_val:
                    continue

                field_id = await field.get_attribute("id") or ""
                field_name = await field.get_attribute("name") or ""
                aria_required = await field.get_attribute("aria-required") or ""

                # Get label via 'for' attribute — Workday always sets this
                label = ""
                if field_id:
                    label_el = await page.query_selector(f"label[for='{field_id}']")
                    if label_el:
                        label = (await label_el.inner_text()).strip()

                label_lower = label.lower()
                name_lower = field_name.lower()

                # Match by name attribute first (more reliable than label)
                value = ""
                if "firstname" in field_name.lower() or "firstname" in field_id.lower():
                    value = PROFILE["first_name"]
                elif "lastname" in field_name.lower() or "lastname" in field_id.lower():
                    value = PROFILE["last_name"]
                elif field_name == "addressLine1":
                    value = PROFILE["address_line1"]
                elif field_name == "city":
                    value = PROFILE["city"]
                elif field_name == "postalCode":
                    value = PROFILE["postal_code"]
                elif field_name == "phoneNumber":
                    value = PROFILE["phone"]
                elif field_name == "extension":
                    value = None  # skip — optional
                else:
                    value = self._match_field(label_lower or name_lower)

                if value is None:
                    continue
                if value:
                    await field.fill(str(value))
                    self.form_snapshot[field_name or label] = value
                    db.log("info", "WORKDAY", f"Filled '{field_name or label}' = '{value}'")
                elif aria_required == "true":
                    self._flag_issue(page, self.page_number, label or field_name, "text", None)

            except Exception as e:
                db.log("warn", "WORKDAY", f"Error filling text field: {e}")


    async def _fill_dropdowns(self, page: Page):
        """Fill Workday dropdowns — both button-style and multiselect search-style."""

        # Type 1 — Multiselect search inputs (How Did You Hear, Country Phone Code)
        multiselects = await page.query_selector_all(
            "input[data-uxi-widget-type='selectinput']"
        )
        for field in multiselects:
            try:
                is_visible = await field.is_visible()
                if not is_visible:
                    continue

                field_id = await field.get_attribute("id") or ""

                # Check if already has a selection
                aria_desc = await field.get_attribute("aria-describedby") or ""
                if aria_desc:
                    desc_el = await page.query_selector(f"[id='{aria_desc}']")
                    if desc_el:
                        desc_text = await desc_el.inner_text()
                        if desc_text and "selected" in desc_text.lower():
                            continue  # already filled

                # Get label
                label = ""
                label_el = await page.query_selector(f"label[for='{field_id}']")
                if label_el:
                    label = (await label_el.inner_text()).strip()

                label_lower = label.lower()

                # Determine value to type
                value = ""
                if "how did you hear" in label_lower or field_id == "source--source":
                    # Check if already selected
                    instruction_el = await page.query_selector(f"[data-automation-id='promptAriaInstruction'][id='{aria_desc}']") if aria_desc else None
                    if instruction_el:
                        instruction_text = await instruction_el.inner_text()
                        if "linkedin" in instruction_text.lower():
                            continue
                    value = "Linkedin"
                elif "country phone" in label_lower or "countryPhoneCode" in field_id:
                    continue  # already set to US by default
                else:
                    value = self._match_screening(label_lower)

                if not value:
                    continue

                await field.click()
                await field.fill(value)
                await page.wait_for_timeout(800)

                # Click first matching option
                option = await page.query_selector(
                    f"[data-automation-id='promptOption']:has-text('Linkedin Jobs'), "
                    f"li[role='option']:has-text('{value}'), "
                    f"div[role='option']:has-text('{value}')"
                )
                if option:
                    await option.click()
                    self.form_snapshot[label] = value
                    db.log("info", "WORKDAY", f"Multiselect '{label}' = '{value}'")
                else:
                    await page.keyboard.press("Escape")
                    db.log("warn", "WORKDAY", f"No option found for multiselect '{label}'")

            except Exception as e:
                db.log("warn", "WORKDAY", f"Multiselect error: {e}")

        # Type 2 — Button dropdowns (Country, State, Phone Device Type)
        buttons = await page.query_selector_all("button[aria-haspopup='listbox']")
        for btn in buttons:
            try:
                is_visible = await btn.is_visible()
                if not is_visible:
                    continue

                btn_name = await btn.get_attribute("name") or ""
                aria_label = await btn.get_attribute("aria-label") or ""
                btn_text = (await btn.inner_text()).strip()

                # Skip utility menu button
                if await btn.get_attribute("data-automation-id") == "utilityMenuButton":
                    continue

                # Skip if already has a real value selected
                aria_lower = (await btn.get_attribute("aria-label") or "").lower()
                is_voluntary = any(x in aria_lower for x in ["ethnicity", "gender", "veteran"])
                if btn_text and btn_text != "Select One" and not is_voluntary:
                    continue

                # Determine value by name attribute
                value = ""
                if btn_name == "country":
                    continue  # already set to US
                elif btn_name == "countryRegion":
                    value = "New York"
                elif btn_name == "phoneType":
                    value = "Home"
                else:
                    value = self._match_screening(aria_label.lower())

                if not value:
                    db.log("warn", "WORKDAY", f"No value for button dropdown '{btn_name}'")
                    continue

                await btn.click()
                await page.wait_for_timeout(800)

                # Find and click option
                option = await page.query_selector(
                    f"li[role='option']:has-text('{value}'), "
                    f"div[role='option']:has-text('{value}'), "
                    f"[data-automation-id='promptOption']:has-text('{value}')"
                )
                if option:
                    await option.click()
                    self.form_snapshot[btn_name] = value
                    db.log("info", "WORKDAY", f"Dropdown '{btn_name}' = '{value}'")
                else:
                    await page.keyboard.press("Escape")
                    db.log("warn", "WORKDAY", f"Option '{value}' not found for '{btn_name}'")

            except Exception as e:
                db.log("warn", "WORKDAY", f"Button dropdown error: {e}")


    async def _fill_radio_checkboxes(self, page: Page):
        """Handle radio buttons and checkboxes."""
        # Workday radio buttons have name attribute — use that for matching
        radio_groups = await page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input[type="radio"]');
                const groups = {};
                inputs.forEach(i => {
                    if (!groups[i.name]) groups[i.name] = [];
                    groups[i.name].push({
                        name: i.name,
                        value: i.value,
                        id: i.id,
                        checked: i.checked
                    });
                });
                return groups;
            }
        """)

        for group_name, radios in radio_groups.items():
            try:
                # Check if any already selected
                if any(r['checked'] for r in radios):
                    continue

                # Determine answer
                value_to_select = ""
                if group_name == "candidateIsPreviousWorker":
                    value_to_select = "false"  # No
                else:
                    label_el = await page.query_selector(f"legend label, [aria-labelledby]")
                    group_label = ""
                    if label_el:
                        group_label = (await label_el.inner_text()).lower()
                    answer = self._match_screening(group_label or group_name.lower())
                    value_to_select = "true" if answer.lower() == "yes" else "false"

                # Click the radio with matching value
                radio = await page.query_selector(
                    f"input[type='radio'][name='{group_name}'][value='{value_to_select}']"
                )
                if radio:
                    # Click the label instead — more reliable
                    radio_id = await radio.get_attribute("id")
                    label = await page.query_selector(f"label[for='{radio_id}']")
                    if label:
                        await label.click()
                    else:
                        await radio.click()
                    self.form_snapshot[group_name] = value_to_select
                    db.log("info", "WORKDAY", f"Radio '{group_name}' = '{value_to_select}'")

            except Exception as e:
                db.log("warn", "WORKDAY", f"Radio error: {e}")

    async def _upload_resume(self, page: Page):
        """Upload resume PDF if a file input is present and not already uploaded."""
        if not self.resume_pdf_path:
            return

        # Check if already uploaded
        already = await page.query_selector("[data-automation-id='file-upload-drop-zone'] + div .css-10klw3m, div:has-text('Successfully Uploaded')")
        if already:
            db.log("info", "WORKDAY", "Resume already uploaded — skipping")
            return

        file_inputs = await page.query_selector_all("input[data-automation-id='file-upload-input-ref']")
        for file_input in file_inputs:
            try:
                await file_input.set_input_files(self.resume_pdf_path)
                db.log("info", "WORKDAY", f"Uploaded resume: {self.resume_pdf_path}")
                await page.wait_for_timeout(2000)
                break
            except Exception as e:
                db.log("warn", "WORKDAY", f"Resume upload error: {e}")

    async def _click_next(self, page: Page) -> bool:
        """Click the Next button to advance to the next form page."""
        selectors = [
            "button[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='nextButton']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Save and Continue')",
        ]
        for sel in selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    return True
            except Exception:
                continue
        return False

    async def _click_submit(self, page: Page) -> bool:
        """Click the final Submit button."""
        selectors = [
            "button[data-automation-id='pageFooterNextButton']:has-text('Submit')",
            "button[data-automation-id='pageFooterNextButton']",
            "button:has-text('Submit')",
        ]
        for sel in selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    db.log("success", "WORKDAY", "Submit button clicked")
                    return True
            except Exception:
                continue
        return False

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _match_field(self, label_lower: str) -> str:
            """Match a field label to a profile value."""
            if "first name" in label_lower:
                return PROFILE["first_name"]
            if "last name" in label_lower or "surname" in label_lower:
                return PROFILE["last_name"]
            if "email" in label_lower:
                return PROFILE["email"]
            if "phone extension" in label_lower:
                return None
            if "phone" in label_lower or "mobile" in label_lower:
                return PROFILE["phone"]
            if "address line 1" in label_lower or "street" in label_lower:
                return PROFILE["address_line1"]
            if "address line 2" in label_lower:
                return ""
            if "postal" in label_lower or "zip" in label_lower:
                return PROFILE["postal_code"]
            if "city" in label_lower:
                return PROFILE["city"]
            if "state" in label_lower:
                return PROFILE["state"]
            if "country" in label_lower:
                return PROFILE["country"]
            if "phone device" in label_lower or "device type" in label_lower:
                return PROFILE["phone_type"]
            if "linkedin" in label_lower:
                return PROFILE["linkedin"]
            if "github" in label_lower or "portfolio" in label_lower:
                return PROFILE["github"]
            if "location" in label_lower:
                return PROFILE["location"]
            if "salary" in label_lower or "compensation" in label_lower:
                return PROFILE["salary_min"]
            if "utilitymenu" in label_lower:
                return ""
            return self._match_screening(label_lower)

    def _match_screening(self, label_lower: str) -> str:
        """Match a label against screening answers."""
        for key, answer in SCREENING_ANSWERS.items():
            if key in label_lower:
                return answer
        return ""

    async def _get_field_label(self, page: Page, field) -> str:
        """Extract the label text for a form field."""
        try:
            # Try aria-label first
            aria = await field.get_attribute("aria-label")
            if aria:
                return aria.strip()

            # Try associated <label> element
            field_id = await field.get_attribute("id")
            if field_id:
                label_el = await page.query_selector(f"label[for='{field_id}']")
                if label_el:
                    text = await label_el.inner_text()
                    return text.strip()

            # Try placeholder
            placeholder = await field.get_attribute("placeholder")
            if placeholder:
                return placeholder.strip()

            # Try data-automation-id
            auto_id = await field.get_attribute("data-automation-id")
            if auto_id:
                return auto_id.replace("-", " ").replace("_", " ").strip()

            # Walk up DOM to find label text
            label_text = await page.evaluate("""
                (el) => {
                    let node = el;
                    for (let i = 0; i < 5; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        const label = node.querySelector('label');
                        if (label) return label.innerText;
                        const legend = node.querySelector('legend');
                        if (legend) return legend.innerText;
                    }
                    return '';
                }
            """, field)
            return label_text.strip() if label_text else ""

        except Exception:
            return ""

    async def _get_dropdown_options(self, page: Page) -> list:
        """Get visible dropdown options for issue reporting."""
        try:
            options = await page.query_selector_all(
                "li[role='option'], div[role='option']"
            )
            texts = []
            for opt in options:
                text = await opt.inner_text()
                texts.append(text.strip())
            return texts[:20]  # cap at 20 options
        except Exception:
            return []

    def _flag_issue(self, page, page_num: int, field_label: str,
                    field_type: str, options):
        """Record an issue for dashboard resolution."""
        issue = {
            "application_id": self.application_id,
            "job_id": self.job['id'],
            "page_number": page_num,
            "field_label": field_label,
            "field_type": field_type,
            "options": options,
            "status": "open",
        }
        self.issues.append(issue)
        db.log("warn", "WORKDAY", f"Issue flagged: '{field_label}' on page {page_num}")

    async def _save_issues(self):
        """Write all flagged issues to Supabase."""
        for issue in self.issues:
            db.insert_issue(issue)
        db.log("info", "WORKDAY", f"Saved {len(self.issues)} issues to database")


# ── REPLAY ────────────────────────────────────────────────────────────────────

async def replay_application(application_id: str, resume_pdf_path: str = None):
    """
    Replay a flagged application after issues have been resolved.
    Loads the saved form snapshot + resolved answers and resubmits.
    """
    app = db.get_application(application_id)
    if not app:
        raise ValueError(f"Application {application_id} not found")

    job = db.get_job(app['job_id'])
    if not job:
        raise ValueError(f"Job {app['job_id']} not found")

    # Load resolved issue answers into SCREENING_ANSWERS
    resolved = db.get_open_issues(application_id)
    for issue in resolved:
        if issue.get('your_answer'):
            SCREENING_ANSWERS[issue['field_label'].lower()] = issue['your_answer']

    db.log("info", "WORKDAY", f"Replaying application for {job['company']}")

    agent = WorkdayAgent(job, application_id, resume_pdf_path)
    return await agent.run()
