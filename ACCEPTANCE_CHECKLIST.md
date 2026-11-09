# Travel Manager NAS — first-install acceptance checklist

This is an installation package, not a deployed service. Keep earlier HTML versions and backups.

## Before installing
- Confirm the exact NAS model and DSM version, and that Container Manager is available.
- Check that company policy permits business confirmations and invoices on a privately owned NAS.
- Back up your existing v2/v3 full JSON backup. The application package contains the original itinerary supplied in this conversation, not additional documents you uploaded on your own device.
- Choose a strong, unique application password. Never use or share your Synology administrator password.
- Use private VPN access and HTTPS. Do not open the app or DSM administration to the public internet as a shortcut.

## After installing
1. Open the configured private HTTPS address in a computer browser. Sign in.
2. Import the latest full v2/v3 JSON backup. Check trip names, manual notes, itinerary items, costs and attachment originals.
3. Upload a clear rental confirmation. Verify pickup/return dates, local time zones, locations, company and reference. Confirm the item appears automatically in the correct trip, with the source attached.
4. Upload an ambiguous confirmation. Confirm it stays in review with a reason instead of being silently assigned to a private/business trip.
5. Edit an itinerary item and a cost; remove a temporary item. Check the saved status.
6. Open the same address on your iPhone in Safari. Use Share → Add to Home Screen if desired. Check that the same data is visible, upload a photo/PDF and confirm the server saved it.
7. Open the itinerary PDF workflow. Verify Safari's print/share steps and the resulting file before relying on it while travelling.
8. Make changes on one device. Check they appear on another idle signed-in device. If both edit concurrently, verify a conflict is shown rather than silently losing changes. Export a backup before discarding conflicting edits.
9. Export Excel and a full JSON backup. Verify originals are present in the JSON backup; they are deliberately absent from Excel cells.
10. Restart the container and verify the same records and originals remain. Test a backup restore using a separate test data directory, not by overwriting your only live copy.

## Boundaries
- One application account sees all business and private trips. Labels do not create separate access permissions.
- Edits save to the NAS; this is not an offline-edit app. Keep the page open when a connection or save fails.
- Local server readers support text/email/PDF and English OCR for images/scans. Extraction rules are conservative, not general AI. Unusual layouts, multiple-leg tables, missing years/time zones and unsupported formats need review.
- Outlook MSG decoding, live booking verification, automatic mailbox monitoring, MFA within this application and granular multi-user permissions are not provided.
- Removing current records does not purge retained server history, old originals or backups. There is no historical restore/purge interface in this release.
- Backups contain sensitive data and are not encrypted by the application. Restrict NAS permissions and secure backups separately.
- Initial container build requires internet access to download its operating-system/Python components. Document reading has no public-library downloads at runtime.

Installation on your actual NAS, reverse-proxy/VPN configuration, and operation on a physical iPhone must still be verified by you. The provided software was tested in an isolated development environment; those tests do not constitute a production security certification.
