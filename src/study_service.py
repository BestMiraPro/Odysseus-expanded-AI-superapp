"""Thin service helpers for Study route ownership checks."""

from fastapi import HTTPException

from core.database import (
    StudyCard,
    StudyDeck,
    StudyExam,
    StudyMaterial,
    StudyQuestion,
)


def get_deck(db, deck_id: str, user) -> StudyDeck:
    deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
    if not deck or (user is not None and deck.owner != user):
        raise HTTPException(404, "Deck not found")
    return deck


def get_card(db, card_id: str, user) -> StudyCard:
    card = db.query(StudyCard).filter(StudyCard.id == card_id).first()
    if not card or (user is not None and card.owner != user):
        raise HTTPException(404, "Card not found")
    return card


def get_exam(db, exam_id: str, user) -> StudyExam:
    exam = db.query(StudyExam).filter(StudyExam.id == exam_id).first()
    if not exam or (user is not None and exam.owner != user):
        raise HTTPException(404, "Exam not found")
    return exam


def get_material(db, material_id: str, user) -> StudyMaterial:
    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material or (user is not None and material.owner != user):
        raise HTTPException(404, "Material not found")
    return material


def get_question(db, question_id: str, user) -> StudyQuestion:
    question = db.query(StudyQuestion).filter(StudyQuestion.id == question_id).first()
    if not question or (user is not None and question.owner != user):
        raise HTTPException(404, "Question not found")
    return question
