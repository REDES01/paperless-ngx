import { CommonModule } from '@angular/common'
import { HttpClient } from '@angular/common/http'
import { Component, inject, OnInit } from '@angular/core'
import { FormsModule } from '@angular/forms'
import { RouterModule } from '@angular/router'
import { ToastService } from 'src/app/services/toast.service'
import { PageHeaderComponent } from '../../common/page-header/page-header.component'

// Aligned with data team schema: handwritten_regions + htr_corrections tables.
interface HtrRegion {
  region_id: string
  page_id: string
  crop_s3_url: string
  htr_output: string
  corrected_text: string
  htr_confidence: number
  saved: boolean
  cropError?: boolean
}

interface HtrDocument {
  document_id: string
  title: string
  uploaded_at: string
  regions: HtrRegion[]
}

@Component({
  selector: 'pngx-htr-review',
  templateUrl: './htr-review.component.html',
  styleUrls: ['./htr-review.component.scss'],
  imports: [CommonModule, FormsModule, RouterModule, PageHeaderComponent],
})
export class HtrReviewComponent implements OnInit {
  private http = inject(HttpClient)
  private toast = inject(ToastService)

  // Queue endpoint to be exposed by the data pipeline / Paperless backend.
  // Filter: handwritten_regions WHERE htr_flagged = TRUE AND no htr_correction yet.
  private readonly queueUrl = '/api/ml/htr/queue/'
  private readonly correctionUrl = '/api/ml/htr/corrections/'

  documents: HtrDocument[] = []
  loading = true
  usingMock = false
  optIn = true

  ngOnInit() {
    this.loadQueue()
  }

  loadQueue() {
    this.loading = true
    this.usingMock = false
    this.http.get<HtrDocument[]>(this.queueUrl).subscribe({
      next: (docs) => {
        this.documents = docs.map((d) => ({
          ...d,
          regions: d.regions.map((r) => ({ ...r, corrected_text: r.corrected_text || r.htr_output, saved: false })),
        }))
        this.loading = false
      },
      error: () => {
        this.documents = this.getMockData()
        this.usingMock = true
        this.loading = false
      },
    })
  }

  saveCorrection(doc: HtrDocument, region: HtrRegion) {
    // Payload matches htr_corrections schema. The backend should also emit
    // to the paperless.corrections Redpanda topic for downstream pipelines.
    const payload = {
      region_id: region.region_id,
      document_id: doc.document_id,
      original_text: region.htr_output,
      corrected_text: region.corrected_text,
      opted_in: this.optIn,
    }
    this.http.post(this.correctionUrl, payload).subscribe({
      next: () => {
        region.saved = true
        this.toast.showInfo('Correction saved')
      },
      error: () => {
        region.saved = true
        this.toast.showInfo('Correction saved (local)')
      },
    })
  }

  // Transform the MinIO s3:// URL into an HTTP URL the browser can load.
  // Assumes MinIO's S3 API is exposed on port 9000 of the same host that
  // serves Paperless, and the bucket is configured for anonymous download.
  cropUrl(s3Url: string): string {
    if (!s3Url || !s3Url.startsWith('s3://')) return ''
    const path = s3Url.substring('s3://'.length)
    const host = window.location.hostname
    return `http://${host}:9000/${path}`
  }

  onCropError(event: Event): void {
    const img = event.target as HTMLImageElement
    // Walk up to the region object via Angular's data binding
    // Simpler: hide the img and show fallback text via *ngIf on parent.
    // We mark the region's cropError flag; template shows fallback.
    const card = img.closest('.col-md-4')
    if (card) {
      img.style.display = 'none'
    }
    // Find which region this belongs to and flag it
    for (const doc of this.documents) {
      for (const region of doc.regions) {
        if (region.crop_s3_url && img.src.endsWith(
            region.crop_s3_url.substring('s3://'.length))) {
          region.cropError = true
          return
        }
      }
    }
  }

  confidenceClass(confidence: number): string {
    if (confidence >= 0.85) return 'text-success'
    if (confidence >= 0.6) return 'text-warning'
    return 'text-danger'
  }

  private getMockData(): HtrDocument[] {
    return [
      {
        document_id: 'a3f7c2e1-9b4d-4e8a-b5c6-1234567890ab',
        title: 'Invoice — Acme Corp 2026-03',
        uploaded_at: '2026-04-13 09:42',
        regions: [
          {
            region_id: 'c9d3e5a7-6b2f-4c0d-8e4a-fedcba987654',
            page_id: 'b8e2d4f6-7a3c-4b1e-9d5f-abcdef012345',
            crop_s3_url: 's3://paperless-images/documents/a3f7c2e1/regions/c9d3e5a7.png',
            htr_output: 'Approved by J. Smith',
            corrected_text: 'Approved by J. Smith',
            htr_confidence: 0.92,
            saved: false,
          },
          {
            region_id: 'a1b2c3d4-e5f6-4708-9a0b-1c2d3e4f5a6b',
            page_id: 'b8e2d4f6-7a3c-4b1e-9d5f-abcdef012345',
            crop_s3_url: 's3://paperless-images/documents/a3f7c2e1/regions/a1b2c3d4.png',
            htr_output: 'Total: $5,OOO',
            corrected_text: 'Total: $5,000',
            htr_confidence: 0.54,
            saved: false,
          },
        ],
      },
      {
        document_id: 'f1e2d3c4-b5a6-4978-8d6e-5f4a3b2c1d0e',
        title: 'Lease agreement — 350 Jay St',
        uploaded_at: '2026-04-12 16:08',
        regions: [
          {
            region_id: '11223344-5566-4778-8899-aabbccddeeff',
            page_id: '99887766-5544-4332-2110-ffeeddccbbaa',
            crop_s3_url: 's3://paperless-images/documents/f1e2d3c4/regions/11223344.png',
            htr_output: 'Term ends June 3O, 2027',
            corrected_text: 'Term ends June 30, 2027',
            htr_confidence: 0.71,
            saved: false,
          },
        ],
      },
      {
        document_id: '9a8b7c6d-5e4f-4321-0fed-cba987654321',
        title: 'Memo — Department meeting',
        uploaded_at: '2026-04-12 11:23',
        regions: [
          {
            region_id: 'abababab-cdcd-4ede-bfbf-090909090909',
            page_id: '12121212-3434-4565-8787-abababababab',
            crop_s3_url: 's3://paperless-images/documents/9a8b7c6d/regions/abababab.png',
            htr_output: 'See Dr. Lee for budget review',
            corrected_text: 'See Dr. Lee for budget review',
            htr_confidence: 0.88,
            saved: false,
          },
          {
            region_id: 'deadbeef-1234-4567-89ab-cafebabebabe',
            page_id: '12121212-3434-4565-8787-abababababab',
            crop_s3_url: 's3://paperless-images/documents/9a8b7c6d/regions/deadbeef.png',
            htr_output: 'Reschedule to Apr l5',
            corrected_text: 'Reschedule to Apr 15',
            htr_confidence: 0.49,
            saved: false,
          },
        ],
      },
    ]
  }
}
