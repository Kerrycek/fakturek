<?php
declare(strict_types=1);

/*
 * Simple Fakturek API v1 example:
 * 1. fetch contacts
 * 2. pick one random contact
 * 3. create invoice draft
 * 4. issue it immediately
 * 5. fetch invoice detail
 * 6. download authenticated PDF to local file
 *
 * Usage:
 *   php api_create_invoice.php
 *
 * Before running, fill in:
 * - FAKTUREK_API_BASE_URL
 * - FAKTUREK_API_TOKEN
 * - FAKTUREK_SUBJECT_ID
 *
 * CONTACT_ID is optional:
 * - set it to a number to force a specific contact
 * - set it to null to choose a random contact automatically
 *
 * Example:
 *   export FAKTUREK_API_BASE_URL="https://invoices.example.com/api/v1"
 *   export FAKTUREK_API_TOKEN="ftk_pat_xxx"
 *   export FAKTUREK_SUBJECT_ID="1"
 *   php api_create_invoice.php
 */

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000/api/v1';
const DEFAULT_SUBJECT_ID = 1;
const CONTACT_ID = null;

$apiBaseUrl = (string) (getenv('FAKTUREK_API_BASE_URL') ?: DEFAULT_API_BASE_URL);
$apiToken = (string) (getenv('FAKTUREK_API_TOKEN') ?: 'PASTE_TOKEN_HERE');
$subjectId = (int) (getenv('FAKTUREK_SUBJECT_ID') ?: DEFAULT_SUBJECT_ID);

function apiRequest(string $method, string $path, ?array $payload = null, array $extraHeaders = []): array
{
    global $apiBaseUrl, $apiToken;
    $url = rtrim($apiBaseUrl, '/') . $path;
    $headers = array_merge(
        [
            'Authorization: Bearer ' . $apiToken,
            'Accept: application/json',
        ],
        $extraHeaders
    );

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_TIMEOUT => 30,
    ]);

    if ($payload !== null) {
        $headers[] = 'Content-Type: application/json';
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    }

    $raw = curl_exec($ch);
    if ($raw === false) {
        throw new RuntimeException('cURL error: ' . curl_error($ch));
    }

    $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    curl_close($ch);

    $decoded = json_decode($raw, true);
    return [
        'status' => $status,
        'body' => $decoded ?? $raw,
    ];
}

function apiDownload(string $path): string
{
    global $apiBaseUrl, $apiToken;
    $url = rtrim($apiBaseUrl, '/') . $path;
    $headers = [
        'Authorization: Bearer ' . $apiToken,
        'Accept: application/pdf',
    ];

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST => 'GET',
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_TIMEOUT => 60,
    ]);

    $raw = curl_exec($ch);
    if ($raw === false) {
        throw new RuntimeException('cURL error: ' . curl_error($ch));
    }

    $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $contentType = (string) curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    curl_close($ch);

    if ($status !== 200) {
        throw new RuntimeException('PDF download failed with HTTP ' . $status . ' (' . $contentType . ')');
    }

    return $raw;
}

if ($apiToken === 'PASTE_TOKEN_HERE') {
    fwrite(STDERR, "Fill in FAKTUREK_API_TOKEN first.\n");
    exit(1);
}

$selectedContactId = CONTACT_ID;
$selectedContactName = null;

if ($selectedContactId === null) {
    $contactsResponse = apiRequest('GET', '/subjects/' . $subjectId . '/contacts');
    if ($contactsResponse['status'] !== 200 || !is_array($contactsResponse['body'])) {
        fwrite(STDERR, "Could not load contacts.\n");
        fwrite(STDERR, print_r($contactsResponse, true));
        exit(1);
    }

    $contacts = $contactsResponse['body']['items'] ?? [];
    if (!is_array($contacts) || $contacts === []) {
        fwrite(STDERR, "No contacts available for subject " . $subjectId . ".\n");
        exit(1);
    }

    $randomIndex = array_rand($contacts);
    $selected = $contacts[$randomIndex];
    $selectedContactId = (int) ($selected['id'] ?? 0);
    $selectedContactName = (string) ($selected['name'] ?? ('Contact #' . $selectedContactId));
} else {
    $contactDetailResponse = apiRequest('GET', '/subjects/' . $subjectId . '/contacts/' . (int) $selectedContactId);
    if ($contactDetailResponse['status'] === 200 && is_array($contactDetailResponse['body'])) {
        $selectedContactName = (string) ($contactDetailResponse['body']['name'] ?? ('Contact #' . $selectedContactId));
    } else {
        $selectedContactName = 'Contact #' . $selectedContactId;
    }
}

$today = new DateTimeImmutable('now', new DateTimeZone('Europe/Prague'));
$due = $today->modify('+14 days');
$idempotencyBase = 'php-example-' . $today->format('Ymd-His') . '-' . bin2hex(random_bytes(4));

$createPayload = [
    'contact_id' => $selectedContactId,
    'issue_date' => $today->format('Y-m-d'),
    'due_date' => $due->format('Y-m-d'),
    'document_type' => 'invoice',
    'payment_method' => 'bank_transfer',
    'items' => [
        [
            'description' => 'API test faktura',
            'quantity' => '1',
            'unit' => 'ks',
            'unit_price' => '100.00',
            'vat_rate' => '0',
        ],
    ],
];

$createResponse = apiRequest(
    'POST',
    '/subjects/' . $subjectId . '/invoices',
    $createPayload,
    ['Idempotency-Key: ' . $idempotencyBase . '-create']
);

if ($createResponse['status'] !== 201) {
    fwrite(STDERR, "Create failed:\n");
    fwrite(STDERR, print_r($createResponse, true));
    exit(1);
}

$invoice = $createResponse['body'];
$invoiceId = (int) ($invoice['id'] ?? 0);

echo "Selected contact:\n";
echo '  ID: ' . $selectedContactId . PHP_EOL;
echo '  Name: ' . $selectedContactName . PHP_EOL;
echo "Draft created:\n";
echo '  ID: ' . $invoiceId . PHP_EOL;
echo '  Number: ' . ($invoice['number'] ?? '-') . PHP_EOL;
echo '  Status: ' . ($invoice['status'] ?? '-') . PHP_EOL;
echo '  Total: ' . ($invoice['total'] ?? '-') . PHP_EOL;

$issueResponse = apiRequest(
    'POST',
    '/subjects/' . $subjectId . '/invoices/' . $invoiceId . '/issue',
    null,
    ['Idempotency-Key: ' . $idempotencyBase . '-issue']
);

if ($issueResponse['status'] !== 200) {
    fwrite(STDERR, "Issue failed:\n");
    fwrite(STDERR, print_r($issueResponse, true));
    exit(1);
}

$issued = $issueResponse['body'];
$detailResponse = apiRequest(
    'GET',
    '/subjects/' . $subjectId . '/invoices/' . $invoiceId
);

if ($detailResponse['status'] !== 200 || !is_array($detailResponse['body'])) {
    fwrite(STDERR, "Invoice detail fetch failed:\n");
    fwrite(STDERR, print_r($detailResponse, true));
    exit(1);
}

$detail = $detailResponse['body'];
$invoiceNumber = (string) ($detail['number'] ?? $issued['number'] ?? ('invoice-' . $invoiceId));
$pdfPath = '/subjects/' . $subjectId . '/invoices/' . $invoiceId . '/pdf';
$pdfBytes = apiDownload($pdfPath);
$localPdfFile = __DIR__ . '/' . preg_replace('/[^A-Za-z0-9._-]+/', '-', $invoiceNumber) . '.pdf';
file_put_contents($localPdfFile, $pdfBytes);

echo "\nIssued invoice:\n";
echo '  ID: ' . ($issued['id'] ?? '-') . PHP_EOL;
echo '  Number: ' . $invoiceNumber . PHP_EOL;
echo '  Status: ' . ($issued['status'] ?? '-') . PHP_EOL;
echo '  Variable symbol: ' . ($issued['variable_symbol'] ?? '-') . PHP_EOL;
echo '  Public URL: ' . (($detail['public_link']['short_url'] ?? $detail['public_link']['url'] ?? 'not available')) . PHP_EOL;
echo '  Public PDF: ' . (($detail['public_link']['pdf_url'] ?? 'not available')) . PHP_EOL;
echo '  Saved PDF: ' . $localPdfFile . PHP_EOL;
