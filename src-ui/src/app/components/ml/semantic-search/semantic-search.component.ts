import { CommonModule } from '@angular/common'
import { HttpClient } from '@angular/common/http'
import { Component, inject } from '@angular/core'
import { FormsModule } from '@angular/forms'
import { RouterModule } from '@angular/router'
import { ToastService } from 'src/app/services/toast.service'
import { PageHeaderComponent } from '../../common/page-header/page-header.component'

type SearchMode = 'semantic' | 'keyword' | 'hybrid'

// Matches serving contract: contracts/search_output.json
interface ServingSearchResult {
  document_id: string
  chunk_index: number
  chunk_text: string
  similarity_score: number
}

interface ServingSearchResponse {
  session_id: string
  query_text: string
  results: ServingSearchResult[]
  fallback_to_keyword: boolean
  model_version: string
  inference_time_ms: number
}

interface DisplayResult extends ServingSearchResult {
  feedback: 'up' | 'down' | null
}

@Component({
  selector: 'pngx-semantic-search',
  templateUrl: './semantic-search.component.html',
  styleUrls: ['./semantic-search.component.scss'],
  imports: [CommonModule, FormsModule, RouterModule, PageHeaderComponent],
})
export class SemanticSearchComponent {
  private http = inject(HttpClient)
  private toast = inject(ToastService)

  // Reverse-proxy path. Wire this to Yikai's fastapi_server via nginx/Django.
  // Until wired, requests fail and the component falls back to mock data.
  private readonly mlApiBase = '/ml-api'

  // Persist a client-side session id so feedback pairs up with searches.
  private readonly sessionId = this.generateSessionId()

  query = ''
  mode: SearchMode = 'semantic'
  results: DisplayResult[] = []
  loading = false
  hasSearched = false
  fallbackToKeyword = false
  modelVersion = ''
  inferenceMs = 0
  usingMock = false

  search() {
    if (!this.query.trim()) return
    this.loading = true
    this.hasSearched = true
    this.results = []
    this.fallbackToKeyword = false
    this.usingMock = false

    const payload = {
      session_id: this.sessionId,
      query_text: this.query,
      user_id: 'anonymous',
      top_k: 10,
    }

    this.http
      .post<ServingSearchResponse>(`${this.mlApiBase}/predict/search`, payload)
      .subscribe({
        next: (resp) => {
          this.results = resp.results.map((r) => ({ ...r, feedback: null }))
          this.fallbackToKeyword = resp.fallback_to_keyword
          this.modelVersion = resp.model_version
          this.inferenceMs = resp.inference_time_ms
          this.loading = false
        },
        error: () => {
          this.results = this.getMockResults(this.query).map((r) => ({ ...r, feedback: null }))
          this.modelVersion = 'mock'
          this.inferenceMs = 0
          this.usingMock = true
          this.loading = false
        },
      })
  }

  recordClick(result: DisplayResult) {
    // Feedback goes to the data pipeline, not the serving layer.
    // Endpoint is not yet wired — fire-and-forget, ignore errors.
    this.http
      .post('/api/ml/search/feedback/', {
        session_id: this.sessionId,
        document_id: result.document_id,
        feedback_type: 'click',
      })
      .subscribe({ error: () => {} })
  }

  rate(result: DisplayResult, signal: 'up' | 'down') {
    result.feedback = signal
    const feedback_type = signal === 'up' ? 'thumbs_up' : 'thumbs_down'
    this.http
      .post('/api/ml/search/feedback/', {
        session_id: this.sessionId,
        document_id: result.document_id,
        feedback_type,
      })
      .subscribe({
        next: () => this.toast.showInfo('Feedback recorded'),
        error: () => this.toast.showInfo('Feedback recorded (local)'),
      })
  }

  scoreClass(score: number): string {
    if (score >= 0.75) return 'badge bg-success'
    if (score >= 0.5) return 'badge bg-warning text-dark'
    return 'badge bg-secondary'
  }

  shortId(docId: string): string {
    return docId.length > 8 ? docId.substring(0, 8) : docId
  }

  private generateSessionId(): string {
    // RFC4122-ish v4
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0
      const v = c === 'x' ? r : (r & 0x3) | 0x8
      return v.toString(16)
    })
  }

  private getMockResults(q: string): ServingSearchResult[] {
    const lower = q.toLowerCase()
    const all: ServingSearchResult[] = [
      {
        document_id: 'a3f7c2e1-9b4d-4e8a-b5c6-1234567890ab',
        chunk_index: 2,
        chunk_text:
          'The fiscal year 2024 budget allocates $2.3M to laboratory equipment upgrades across three departments.',
        similarity_score: 0.91,
      },
      {
        document_id: 'f1e2d3c4-b5a6-4978-8d6e-5f4a3b2c1d0e',
        chunk_index: 0,
        chunk_text:
          "Annual budget summary for the College of Engineering, prepared for the provost's review.",
        similarity_score: 0.84,
      },
      {
        document_id: '9a8b7c6d-5e4f-4321-0fed-cba987654321',
        chunk_index: 5,
        chunk_text:
          'Departmental spending report Q3-Q4 2024 showing travel and conference expenditures.',
        similarity_score: 0.79,
      },
      {
        document_id: 'd4e5f6a7-8b9c-4d0e-1f2a-3b4c5d6e7f8a',
        chunk_index: 1,
        chunk_text:
          'The term of this lease ends June 30, 2027 with renewal option for two additional years.',
        similarity_score: 0.72,
      },
      {
        document_id: 'e5f6a7b8-9c0d-4e1f-2a3b-4c5d6e7f8a9b',
        chunk_index: 3,
        chunk_text:
          'Invoice from Acme Corp for March services. Total amount due $5,000 payable within 30 days.',
        similarity_score: 0.58,
      },
    ]
    if (lower.includes('lease') || lower.includes('june')) return [all[3], all[2], all[1]]
    if (lower.includes('invoice') || lower.includes('payment')) return [all[4], all[0], all[1]]
    return all
  }
}
